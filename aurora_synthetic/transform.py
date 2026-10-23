"""Build the five contract datasets from simulated sources, mirroring the dbt
models the contracts name. Field-level lineage is documented in
synthetic/lineage.yaml; the function implementing each field is named there.

Incremental semantics (ADR-003, A-OPS-03): daily partition d is rebuilt until
the build `late_window_days` after it, so an event counts toward partition d
only if it was ingested by that partition's last build. Events that arrive
later are picked up by subsequent partitions' trailing windows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from aurora_contracts.models import Dataset, DataType
from aurora_mlops.chunking import chunk_transcript

from .simulate import World

REDACTION_VERSION = "r1"
MART_DAYS = 14
HOURLY_DAYS = 7
FEATURE_VERSION = "1.0.0"
K_MIN = 10  # ABAC-08 small-cell suppression
NUMERIC_FEATURES = ["tenure_days", "orders_90d", "skips_90d", "pauses_180d", "failed_payments_30d", "sessions_30d",
                    "cancel_page_views_14d", "email_open_rate_30d", "support_contacts_90d", "days_since_last_order"]
LIFECYCLE = ["subscription_created", "subscription_paused", "subscription_skipped", "subscription_resumed",
             "subscription_cancelled", "cancel_page_view", "payment_failed"]


@dataclass
class Built:
    datasets: dict[str, pd.DataFrame]
    producer_payloads: list[dict]
    model_report: dict
    redaction_log: pd.DataFrame


# ------------------------------------------------------------------ helpers
def conform(df: pd.DataFrame, ds: Dataset) -> pd.DataFrame:
    """Order and type columns exactly as the contract declares."""
    out = pd.DataFrame(index=df.index)
    for c in ds.columns:
        s = df[c.name] if c.name in df else pd.Series([None] * len(df), index=df.index)
        if c.type is DataType.INTEGER:
            s = s.astype("Int64") if c.nullable else s.astype("int64")
        elif c.type is DataType.NUMBER:
            s = s.astype("float64")
        elif c.type is DataType.BOOLEAN:
            s = s.astype("boolean")
        elif c.type is DataType.TIMESTAMP:
            s = pd.to_datetime(s, utc=True)
        elif c.type is DataType.STRING:
            s = s.astype(object).where(s.notna(), None)
        out[c.name] = s
    return out.reset_index(drop=True)


class Grid:
    """Per-subscriber daily count matrices with incremental-availability semantics."""

    def __init__(self, world: World, raw: pd.DataFrame):
        cfg = world.cfg
        self.cfg, self.days = cfg, cfg.days
        self.subs = world.subscribers["subscriber_id"].tolist()
        self.ix = {s: i for i, s in enumerate(self.subs)}
        ev = raw[raw["subscriber_id"].notna() & raw["event_type"].notna()].drop_duplicates("event_id")
        start = pd.Timestamp(cfg.start_date, tz=cfg.zone)
        local_t = ev["event_time"].dt.tz_convert(cfg.zone)
        self.e_day = ((local_t - start) // pd.Timedelta(days=1)).to_numpy()
        ingest_shift = ev["ingested_at"].dt.tz_convert(cfg.zone) - pd.Timedelta(hours=5)
        avail = ((ingest_shift - start) // pd.Timedelta(days=1)).to_numpy() - cfg.late_window_days
        self.eff_day = np.maximum(self.e_day, avail)
        self.ev = ev.assign(_sub=ev["subscriber_id"].map(self.ix).to_numpy(), _e=self.e_day, _eff=self.eff_day)

    def counts(self, mask: pd.Series, by: str = "_eff", distinct_days: bool = False) -> np.ndarray:
        e = self.ev[mask.reindex(self.ev.index, fill_value=False)]
        e = e[(e[by] >= 0) & (e[by] < self.days)]
        m = np.zeros((len(self.subs), self.days), dtype=np.int64)
        if distinct_days:
            pairs = e[["_sub", by]].drop_duplicates().to_numpy()
            m[pairs[:, 0], pairs[:, 1]] = 1
        else:
            np.add.at(m, (e["_sub"].to_numpy(), e[by].to_numpy()), 1)
        return m

    @staticmethod
    def window(m: np.ndarray, d: int, w: int) -> np.ndarray:
        lo = max(0, d - w + 1)
        return m[:, lo:d + 1].sum(axis=1)

    @staticmethod
    def last_day(m: np.ndarray, d: int) -> np.ndarray:
        hits = m[:, : d + 1] > 0
        any_ = hits.any(axis=1)
        last = d - np.argmax(hits[:, ::-1], axis=1)
        return np.where(any_, last, -1)


# ---------------------------------------------------------------- datasets
def build_raw(world: World, ds: Dataset) -> pd.DataFrame:
    """raw.subscription_events: events as landed by the 05:00 build (later arrivals not yet visible)."""
    build_utc = pd.Timestamp(world.notes["build_time"]).tz_convert("UTC")
    ev = world.events[world.events["ingested_at"] <= build_utc]
    return conform(ev, ds)


def producer_payloads(raw: pd.DataFrame, n: int = 2000, seed: int = 0) -> list[dict]:
    sample = raw[raw["event_type"].notna()].sample(min(n, len(raw)), random_state=seed)
    out = []
    for r in sample.itertuples(index=False):
        out.append({
            "event_id": r.event_id, "event_type": r.event_type, "source": r.source,
            "event_time": r.event_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "subscriber_id": r.subscriber_id,
            "anonymous_id": r.anonymous_id, "ip_address": r.ip_address, "user_agent": r.user_agent,
            "properties": json.loads(r.properties),
        })
    return out


def feature_rows(world: World, g: Grid, dates: list[int], include_cancelled: bool) -> pd.DataFrame:
    s = world.subscribers
    start_day = s["start_day"].to_numpy()
    t = g.ev["event_type"]
    web = g.ev["source"].isin(["web", "app"])
    M = {k: g.counts(t == k) for k in ("payment_succeeded", "payment_failed", "subscription_skipped",
                                        "subscription_paused", "subscription_resumed", "cancel_page_view",
                                        "email_sent", "email_opened", "subscription_cancelled")}
    sessions = g.counts(web, distinct_days=True)
    cancel_actual = g.counts(t == "subscription_cancelled", by="_e")
    tk = world.tickets
    tickets = np.zeros_like(sessions)
    tday = ((tk["opened_at"].map(lambda x: x.astimezone(world.cfg.zone).date()) - world.cfg.start_date)
            .map(lambda x: x.days)).to_numpy()
    tsub = tk["subscriber_id"].map(g.ix).to_numpy()
    ok = (tday >= 0) & (tday < g.days)
    np.add.at(tickets, (tsub[ok], tday[ok]), 1)

    rows = []
    for d in dates:
        cancelled_by = Grid.window(M["subscription_cancelled"], d, d + 1) > 0
        cancel_last = Grid.last_day(M["subscription_cancelled"], d)
        started = start_day <= d
        keep = started & (~cancelled_by | (include_cancelled & (d - cancel_last <= 30)))
        failed7, paid7 = Grid.window(M["payment_failed"], d, 7), Grid.window(M["payment_succeeded"], d, 7)
        paused28 = Grid.window(M["subscription_paused"], d, 28) > Grid.window(M["subscription_resumed"], d, 28)
        status = np.where(cancelled_by, "cancelled", np.where(paused28, "paused",
                          np.where((failed7 > 0) & (paid7 == 0), "past_due", "active")))
        last_order = Grid.last_day(M["payment_succeeded"], d)
        sent30, open30 = Grid.window(M["email_sent"], d, 30), Grid.window(M["email_opened"], d, 30)
        matured = d + 30 <= g.days - 1
        future_cancel = cancel_actual[:, d + 1: min(d + 31, g.days)].sum(axis=1) > 0
        frame = pd.DataFrame({
            "subscriber_id": s["subscriber_id"], "_day": d,
            "date": world.cfg.start_date + timedelta(days=d),
            "plan_interval": s["plan_interval"], "subscription_status": status,
            "tenure_days": d - start_day,
            "orders_90d": Grid.window(M["payment_succeeded"], d, 90),
            "skips_90d": Grid.window(M["subscription_skipped"], d, 90),
            "pauses_180d": Grid.window(M["subscription_paused"], d, 180),
            "failed_payments_30d": Grid.window(M["payment_failed"], d, 30),
            "sessions_30d": Grid.window(sessions, d, 30),
            "cancel_page_views_14d": Grid.window(M["cancel_page_view"], d, 14),
            "email_open_rate_30d": np.where(sent30 > 0, np.minimum(open30 / np.maximum(sent30, 1), 1.0), np.nan),
            "support_contacts_90d": Grid.window(tickets, d, 90),
            "days_since_last_order": np.where(last_order >= 0, d - last_order, -1),
            "label_churned_30d": np.where(matured, future_cancel, None),
            "ship_state": s["ship_state"],
        })[keep]
        rows.append(frame)
    df = pd.concat(rows, ignore_index=True)
    df["days_since_last_order"] = df["days_since_last_order"].where(df["days_since_last_order"] >= 0, pd.NA)
    df["email_open_rate_30d"] = df["email_open_rate_30d"].astype(float)
    return df


def _model_matrix(df: pd.DataFrame) -> pd.DataFrame:
    x = df[NUMERIC_FEATURES].astype(float).fillna(-1)
    for col, levels in (("plan_interval", ["monthly", "bimonthly", "quarterly"]),
                        ("subscription_status", ["active", "paused", "past_due"])):
        for lv in levels:
            x[f"{col}={lv}"] = (df[col] == lv).astype(float)
    return x


def train_churn_model(features: pd.DataFrame) -> tuple[object, dict]:
    labeled = features[features["label_churned_30d"].notna()]
    dates = sorted(labeled["_day"].unique())
    if len(dates) < 2:
        raise ValueError("need at least two feature dates with matured labels; increase --days")
    train, test = labeled[labeled["_day"].isin(dates[:-1])], labeled[labeled["_day"] == dates[-1]]
    # Unweighted: scores are read as probabilities by the retention team (ECE KPI).
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(_model_matrix(train), train["label_churned_30d"].astype(int))
    report = {
        "train_dates": [str(train["date"].min()), str(train["date"].max())], "test_date": str(test["date"].iloc[0]),
        "train_rows": len(train), "test_rows": len(test), "test_positives": int(test["label_churned_30d"].astype(int).sum()),
        "y_test": test["label_churned_30d"].astype(int).tolist(),
        "p_test": model.predict_proba(_model_matrix(test))[:, 1].tolist(),
    }
    return model, report


def build_features(world: World, g: Grid, ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame, object, dict]:
    last = g.days - 1
    dates = sorted(range(last, 90, -7))
    raw_feats = feature_rows(world, g, dates, include_cancelled=False)
    model, report = train_churn_model(raw_feats)
    loaded = pd.Timestamp(world.notes["build_time"]).tz_convert("UTC") + pd.Timedelta(minutes=40)
    out = raw_feats.assign(feature_date=raw_feats["date"], feature_version=FEATURE_VERSION, _loaded_at=loaded)
    return conform(out, ds), raw_feats, model, report


def build_mart(world: World, g: Grid, model, ds: Dataset) -> pd.DataFrame:
    dates = list(range(g.days - MART_DAYS, g.days))
    df = feature_rows(world, g, dates, include_cancelled=True)
    scorable = df["subscription_status"] != "cancelled"
    score = pd.Series(np.nan, index=df.index)
    score[scorable] = model.predict_proba(_model_matrix(df[scorable]))[:, 1]
    band = pd.cut(score, [-0.01, 0.3, 0.6, 1.0], labels=["low", "medium", "high"]).astype(object)
    build = pd.Timestamp(world.notes["build_time"]).tz_convert("UTC")
    loaded = [build - pd.Timedelta(days=(g.days - 1 - d)) + pd.Timedelta(minutes=50) for d in df["_day"]]
    out = df.assign(as_of_date=df["date"], churn_risk_score=score.round(4), churn_risk_band=band.where(score.notna(), None),
                    _loaded_at=loaded)
    return conform(out, ds)


def build_hourly(world: World, raw: pd.DataFrame, ds: Dataset) -> pd.DataFrame:
    cfg = world.cfg
    since = pd.Timestamp(cfg.end_date - timedelta(days=HOURLY_DAYS), tz=cfg.zone).tz_convert("UTC")
    ev = raw[raw["event_type"].isin(LIFECYCLE) & raw["subscriber_id"].notna() & (raw["event_time"] >= since)]
    ev = ev.drop_duplicates("event_id").merge(world.subscribers[["subscriber_id", "ship_state"]], on="subscriber_id")
    ev = ev.assign(event_hour=ev["event_time"].dt.floor("h"))
    keys = ["event_hour", "event_type"]
    cell = ev.groupby([*keys, "ship_state"]).agg(subscriber_count=("subscriber_id", "nunique"), event_count=("event_id", "size")).reset_index()
    big = cell[cell["subscriber_count"] >= K_MIN]
    small_keys = cell[cell["subscriber_count"] < K_MIN][[*keys, "ship_state"]]
    small_ev = ev.merge(small_keys, on=[*keys, "ship_state"])
    rolled = small_ev.groupby(keys).agg(subscriber_count=("subscriber_id", "nunique"), event_count=("event_id", "size")).reset_index()
    rolled = rolled[rolled["subscriber_count"] >= K_MIN].assign(ship_state="suppressed")
    out = pd.concat([big, rolled], ignore_index=True).sort_values(keys)
    out["_loaded_at"] = pd.Timestamp(world.notes["build_time"]).tz_convert("UTC")
    world.notes["hourly_cells_total"] = len(cell)
    world.notes["hourly_cells_suppressed_or_dropped"] = int((cell["subscriber_count"] < K_MIN).sum())
    return conform(out, ds)


# ------------------------------------------------------------- RAG corpus
CARD_GROUPED = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")


def redact(text: str, profile: pd.Series) -> tuple[str, list[str]]:
    """Redactor r1: profile-based names/emails/phones, generic phones, grouped card numbers.

    Known gaps by design (seeded EC-02, EC-03): emails not on the profile and
    card numbers typed without separators.
    """
    hits = []
    for value, token in ((profile["email"], "[EMAIL]"), (f"{profile['first_name']} {profile['last_name']}", "[NAME]"),
                         (profile["first_name"], "[NAME]"), (profile["last_name"], "[NAME]"), (profile["phone"], "[PHONE]")):
        if value and value in text:
            text = text.replace(value, token)
            hits.append(token)
    for rx, token in ((CARD_GROUPED, "[CARD]"), (PHONE, "[PHONE]")):
        text, k = rx.subn(token, text)
        hits += [token] * k
    return text, hits


def build_chunks(world: World, ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    prof = world.subscribers.set_index("subscriber_id")
    rows, log = [], []
    build = pd.Timestamp(world.notes["build_time"]).tz_convert("UTC")
    for t in world.tickets.itertuples(index=False):
        text, hits = redact(t.transcript_raw, prof.loc[t.subscriber_id])
        log.append({"ticket_id": t.ticket_id, "redactions": len(hits)})
        for c in chunk_transcript(t.ticket_id, text, REDACTION_VERSION, max_tokens=512):
            rows.append({"chunk_id": c.chunk_id, "ticket_id": t.ticket_id, "subscriber_id": t.subscriber_id,
                         "chunk_index": c.chunk_index, "chunk_text_redacted": c.text, "token_count": c.token_count,
                         "topic_tags": [t.topic], "ticket_closed_at": pd.Timestamp(t.closed_at).tz_convert("UTC"),
                         "redaction_version": REDACTION_VERSION, "_loaded_at": build + pd.Timedelta(minutes=45)})
    return conform(pd.DataFrame(rows), ds), pd.DataFrame(log)


def build_all(world: World, contracts) -> Built:
    d = contracts.datasets
    raw = build_raw(world, d["raw.subscription_events"])
    g = Grid(world, raw)
    features, _, model, report = build_features(world, g, d["features.churn_features"])
    mart = build_mart(world, g, model, d["marts.retention_subscription_health_daily"])
    hourly = build_hourly(world, raw, d["marts.retention_events_hourly"])
    chunks, log = build_chunks(world, d["rag.transcript_chunks"])
    return Built(
        datasets={"raw.subscription_events": raw, "marts.retention_subscription_health_daily": mart,
                  "marts.retention_events_hourly": hourly, "features.churn_features": features,
                  "rag.transcript_chunks": chunks},
        producer_payloads=producer_payloads(raw, seed=world.cfg.seed),
        model_report=report,
        redaction_log=log,
    )
