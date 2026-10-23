"""Validate the written specs against the generated synthetic data.

Reads synthetic/data/ (run scripts/generate_synthetic.py first) and checks:

  1. Contract conformance: every dataset passes its governance rules except the
     rows the seeded edge cases are meant to trip; plus dbt-test equivalents
     (keys, accepted values, ranges) and the producer JSON Schema.
  2. Seeded edge cases: each fires exactly the rule and failure behavior the
     specs prescribe, on exactly the seeded rows.
  3. Scenarios: gate not cleared, consent-feed outage, stale partitions, schema
     drift, landing delay, incident purge.
  4. Incremental semantics (ADR-003 / A-OPS-03) recomputed independently.
  5. MLOps / RAGOps code paths: churn KPIs and go-live gate, drift, retrieval eval.
  6. Calibration against the assumptions register.
  7. Lineage coverage (synthetic/lineage.yaml) for every contract column.

Writes docs/synthetic_validation_report.md and docs/lineage.md. Exits non-zero
if any required check fails; informational results are reported only.

Usage:
    python scripts/validate_synthetic.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import numpy as np
import pandas as pd
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora_contracts import codegen  # noqa: E402
from aurora_contracts.governance import CheckContext, evaluate  # noqa: E402
from aurora_contracts.loader import load as load_contracts  # noqa: E402
from aurora_mlops import drift, metrics  # noqa: E402
from aurora_mlops.gates import release_gate  # noqa: E402
from build_assumptions import load_assumptions  # noqa: E402

DATA = ROOT / "synthetic" / "data"
LINEAGE = ROOT / "synthetic" / "lineage.yaml"
REPORT = ROOT / "docs" / "synthetic_validation_report.md"
LINEAGE_MD = ROOT / "docs" / "lineage.md"


@dataclass
class Check:
    section: str
    id: str
    spec: str
    expected: str
    actual: str
    passed: bool
    required: bool = True


class Validator:
    def __init__(self, data: Path = DATA, report: Path = REPORT, lineage_md: Path = LINEAGE_MD):
        self.data, self.report, self.lineage_md = data, report, lineage_md
        self.checks: list[Check] = []
        self.m = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
        self.contracts = load_contracts()
        _, self.a = load_assumptions()
        self.d = {n: pd.read_parquet(data / f"{n}.parquet") for n in self.contracts.datasets}
        self.src = {p.stem: pd.read_parquet(p) for p in (data / "sources").glob("*.parquet")}
        self.truth = pd.read_parquet(data / "_truth.parquet")
        cfg = self.m["config"]
        self.zone = ZoneInfo(cfg["tz"])
        self.end = datetime.fromisoformat(self.m["build_time"]).date()
        self.days = int(cfg["days"])
        self.start = self.end - timedelta(days=self.days)
        self.window = int(cfg["late_window_days"])
        self.build_time = datetime.fromisoformat(self.m["build_time"])
        self.ec = {c["id"]: c for c in self.m["edge_cases"]}
        self.consent = self.src["consent.ledger"].set_index("subscriber_id")[["analytics_objection", "ai_processing", "deletion_pending"]]
        self.outcomes = {}

    def add(self, section, id_, spec, expected, actual, passed, required=True):
        self.checks.append(Check(section, id_, spec, str(expected), str(actual), bool(passed), required))

    def ctx(self, name: str, **kw) -> CheckContext:
        local_morning = datetime.combine(self.end, time(8), self.zone)
        now = {
            "raw.subscription_events": self.d["raw.subscription_events"]["ingested_at"].max().to_pydatetime() + timedelta(minutes=5),
            "marts.retention_events_hourly": self.build_time + timedelta(minutes=30),
        }.get(name, local_morning)
        args = dict(now=now, local_tz=self.zone, consent=self.consent, gates_cleared=frozenset({"SEC-G3"}))
        args.update(kw)
        return CheckContext(**args)

    def day(self, d: int):
        return self.start + timedelta(days=d)

    # ------------------------------------------------------------ 1 + 2
    def contracts_and_edge_cases(self):
        sec = "Contracts and seeded edge cases"
        for name, ds in self.contracts.datasets.items():
            df = self.d[name]
            out = evaluate(df, ds, self.ctx(name))
            self.outcomes[name] = out
            failed = {r.rule_id: r for r in out.failed}
            expected_rules = {r for c in self.ec.values() if c["rule"] for r in c["rule"].split(", ")
                              if r.startswith(_prefix(name))}
            unexpected = sorted(set(failed) - expected_rules)
            self.add(sec, f"{name}: rules", "Only seeded edge cases trip governance rules",
                     f"failures only in {sorted(expected_rules) or 'none'}", f"failed: {sorted(failed) or 'none'}", not unexpected)
            missing = sorted(expected_rules - set(failed))
            if missing:
                self.add(sec, f"{name}: seeded", "Every seeded rule fires", "all fire", f"did not fire: {missing}", False)

        # Raw events: exactly the seeded rows are quarantined, by the right rules.
        raw_out = self.outcomes["raw.subscription_events"]
        q = raw_out.quarantined
        for ec, rule in (("EC-05", "GR-EVT-03"), ("EC-08", "GR-EVT-02"), ("EC-09", "GR-EVT-04")):
            eid = self.ec[ec]["keys"]["event_id"]
            hit = q[q["event_id"] == eid]
            ok = len(hit) == 1 and hit["_quarantine_reason"].iloc[0] == rule
            self.add(sec, ec, self.ec[ec]["spec"], f"1 row quarantined by {rule}",
                     f"{len(hit)} row(s), reason {hit['_quarantine_reason'].tolist()}", ok)
        self.add(sec, "raw: publish", "Quarantine does not block landing", "publish = True, 3 rows quarantined",
                 f"publish = {raw_out.publish}, {len(q)} quarantined", raw_out.publish and len(q) == 3)

        # Consent scope: exact subscriber sets per purpose.
        p5_bad = {self.ec[i]["keys"]["subscriber_id"] for i in ("EC-01", "EC-04", "EC-13")}
        p8_bad = {self.ec[i]["keys"]["subscriber_id"] for i in ("EC-04", "EC-10", "EC-13")}
        for name, rule, bad in (("marts.retention_subscription_health_daily", "GR-RET-05", p5_bad),
                                ("features.churn_features", "GR-FEA-05", p5_bad),
                                ("rag.transcript_chunks", "GR-RAG-05", p8_bad)):
            df, out = self.d[name], self.outcomes[name]
            res = next(r for r in out.results if r.rule_id == rule)
            got = set(df.loc[res.failing_rows, "subscriber_id"])
            want = bad & set(df["subscriber_id"])
            self.add(sec, f"{rule} (EC-01/04/10/13)", "Consent scope fails closed on exactly the out-of-scope subscribers",
                     f"{sorted(want)} quarantined", f"{sorted(got)} -> {res.behavior.value if res.behavior else 'pass'}",
                     got == want and res.behavior and res.behavior.value == "quarantine_rows")

        # RAG PII: EC-02 quarantine-level hit, EC-03 incident.
        rag, out = self.d["rag.transcript_chunks"], self.outcomes["rag.transcript_chunks"]
        res = next(r for r in out.results if r.rule_id == "GR-RAG-03")
        tickets = set(rag.loc[res.failing_rows, "ticket_id"])
        want = {self.ec["EC-02"]["keys"]["ticket_id"], self.ec["EC-03"]["keys"]["ticket_id"]}
        self.add(sec, "EC-02 + EC-03", "Residual PII is caught after redaction; card number escalates",
                 f"tickets {sorted(want)}; behavior security_incident; batch blocked",
                 f"tickets {sorted(tickets)}; behavior {res.behavior.value if res.behavior else 'pass'}; publish {out.publish}; incidents {len(out.incidents)}",
                 tickets == want and res.behavior and res.behavior.value == "security_incident" and not out.publish and out.incidents)

        # EC-11 producer schema.
        schema = codegen.event_json_schema(self.contracts.datasets["raw.subscription_events"])
        payloads = [json.loads(l) for l in (self.data / "producer_payloads.jsonl").read_text(encoding="utf-8").splitlines()]
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        bad = [p["event_id"] for p in payloads if not validator.is_valid(p)]
        self.add(sec, "Producer schema", "Every sampled producer payload matches event contract v1",
                 f"{len(payloads)} valid", f"{len(payloads) - len(bad)} valid", not bad)
        rejected = not validator.is_valid(self.ec["EC-11"]["keys"]["payload"])
        self.add(sec, "EC-11", self.ec["EC-11"]["spec"], "rejected", "rejected" if rejected else "accepted", rejected)

        # EC-12 small cells.
        h = self.d["marts.retention_events_hourly"]
        hc = self.m["hourly_cells"]
        self.add(sec, "EC-12", self.ec["EC-12"]["spec"], "min subscriber_count >= 10",
                 f"min {h['subscriber_count'].min() if len(h) else 'n/a'}; {len(h)} published of {hc['total']} raw cells "
                 f"({hc['below_k_before_rollup']} below 10 before roll-up); suppressed rows {int((h['ship_state'] == 'suppressed').sum())}",
                 len(h) == 0 or h["subscriber_count"].min() >= 10)

        # EC-16 health-inferable data never reaches features.
        feats = self.contracts.datasets["features.churn_features"]
        orders = self.src["commerce.orders"]
        self.add(sec, "EC-16", self.ec["EC-16"]["spec"], "health SKUs in source; 0 health/SKU columns in features",
                 f"{int(orders['health_inferable'].sum())} health-inferable orders; health-tagged columns: "
                 f"{[c.name for c in feats.columns if c.health_tagged or 'sku' in c.name or 'category' in c.name] or 'none'}",
                 orders["health_inferable"].any() and not any(c.health_tagged for c in feats.columns))

    # ---------------------------------------------------------- dbt-style
    def column_tests(self):
        sec = "Contract column tests (dbt equivalents)"
        for name, ds in self.contracts.datasets.items():
            df = self.d[name]
            problems = []
            if name != "raw.subscription_events" and df.duplicated(ds.primary_key).any():
                problems.append(f"duplicate {ds.primary_key}")
            for c in ds.columns:
                s = df[c.name].dropna()
                if c.accepted_values and not s.isin(c.accepted_values).all():
                    problems.append(f"{c.name} outside accepted values: {sorted(set(s) - set(c.accepted_values))[:3]}")
                if c.min_value is not None and (s < c.min_value).any():
                    problems.append(f"{c.name} < {c.min_value:g}")
                if c.max_value is not None and (s > c.max_value).any():
                    problems.append(f"{c.name} > {c.max_value:g}")
            if name == "raw.subscription_events":
                extra = df["event_id"].duplicated().sum()
                problems = [p for p in problems if not p.startswith("event_type outside")] + \
                           ([f"{extra - 1} unseeded duplicate event_ids"] if extra != 1 else [])
            self.add(sec, name, "Keys unique; accepted values and ranges hold", "no violations", "; ".join(problems) or "none", not problems)
        cols = {n: list(df.columns) for n, df in self.d.items()}
        exact = all(cols[n] == [c.name for c in ds.columns] for n, ds in self.contracts.datasets.items())
        self.add(sec, "Column order", "Generated tables match contract column names and order", "exact", "exact" if exact else "mismatch", exact)

    # --------------------------------------------------------- scenarios
    def scenarios(self):
        sec = "Scenarios"
        rag, feats = self.d["rag.transcript_chunks"], self.d["features.churn_features"]
        rag_ds, feat_ds = self.contracts.datasets["rag.transcript_chunks"], self.contracts.datasets["features.churn_features"]

        out = evaluate(rag, rag_ds, self.ctx("rag.transcript_chunks", gates_cleared=frozenset()))
        r = next(x for x in out.results if x.rule_id == "GR-RAG-05")
        self.add(sec, "VS-01", "RAG blocked until SEC-G3 clears (ADR-004 G0)", "block_publish", r.behavior.value, r.behavior.value == "block_publish" and not out.publish)

        purged = rag[rag["ticket_id"] != self.ec["EC-03"]["keys"]["ticket_id"]]
        out = evaluate(purged, rag_ds, self.ctx("rag.transcript_chunks"))
        r = next(x for x in out.results if x.rule_id == "GR-RAG-03")
        self.add(sec, "VS-02", "After purging the incident chunk, the batch publishes with EC-02 withheld",
                 "publish; GR-RAG-03 quarantine_rows", f"publish {out.publish}; {r.behavior.value if r.behavior else 'pass'}",
                 out.publish and r.behavior and r.behavior.value == "quarantine_rows")

        ids = self.consent.index.to_numpy()
        outage = self.consent.drop(index=ids[: int(len(ids) * 0.05)])
        out = evaluate(feats, feat_ds, self.ctx("features.churn_features", consent=outage))
        r = next(x for x in out.results if x.rule_id == "GR-FEA-05")
        self.add(sec, "VS-03", "Consent-feed outage (5% missing) escalates to block (GR-FEA-05 > 1%)", "block_publish",
                 r.behavior.value, r.behavior.value == "block_publish" and not out.publish)

        late = self.ctx("features.churn_features", now=datetime.combine(self.end + timedelta(days=2), time(8), self.zone))
        first = evaluate(feats, feat_ds, late)
        r1 = next(x for x in first.results if x.rule_id == "GR-FEA-04")
        late.consecutive_failures = {"GR-FEA-04": 1}
        second = evaluate(feats, feat_ds, late)
        r2 = next(x for x in second.results if x.rule_id == "GR-FEA-04")
        self.add(sec, "VS-04", "Stale features: serve stale, then block on the second miss (CON-ML)",
                 "serve_stale_and_alert -> block_publish", f"{r1.behavior.value} -> {r2.behavior.value}",
                 r1.behavior.value == "serve_stale_and_alert" and r2.behavior.value == "block_publish")

        drifted = feats.assign(new_col=1)
        out = evaluate(drifted, feat_ds, self.ctx("features.churn_features"))
        self.add(sec, "VS-05", "Schema drift blocks publish and skips row checks", "block; 1 rule run",
                 f"publish {out.publish}; {len(out.results)} rule(s) run", not out.publish and len(out.results) == 1)

        raw = self.d["raw.subscription_events"]
        ctx = self.ctx("raw.subscription_events", now=raw["ingested_at"].max().to_pydatetime() + timedelta(minutes=45))
        out = evaluate(raw.head(5000), self.contracts.datasets["raw.subscription_events"], ctx)
        r = next(x for x in out.results if x.rule_id == "GR-EVT-05")
        self.add(sec, "VS-06", "Landing delay of 45 min warns without blocking (GR-EVT-05)", "warn",
                 r.behavior.value if r.behavior else "pass", r.behavior is not None and r.behavior.value == "warn")

    # ------------------------------------------------- incremental logic
    def incremental(self):
        sec = "Incremental semantics (ADR-003, A-OPS-03)"
        raw, mart = self.d["raw.subscription_events"], self.d["marts.retention_subscription_health_daily"]

        def independent(sub: str, d: int) -> tuple[int, set]:
            day = self.day(d)
            cutoff = min(datetime.combine(day + timedelta(days=1 + self.window), time(5), self.zone), self.build_time)
            e = raw[(raw["subscriber_id"] == sub) & (raw["event_type"] == "cancel_page_view")].drop_duplicates("event_id")
            local = e["event_time"].dt.tz_convert(self.zone).dt.date
            keep = e[(local <= day) & (local > day - timedelta(days=14)) & (e["ingested_at"] <= pd.Timestamp(cutoff))]
            return len(keep), set(keep["event_id"])

        def mart_value(sub: str, d: int):
            row = mart[(mart["subscriber_id"] == sub) & (mart["as_of_date"] == self.day(d))]
            return int(row["cancel_page_views_14d"].iloc[0]) if len(row) else None

        for ec in ("EC-06", "EC-07"):
            k = self.ec[ec]["keys"]
            sub, d, eid = k["subscriber_id"], k["event_day"], k["event_id"]
            n, ids = independent(sub, d)
            got = mart_value(sub, d)
            if ec == "EC-06":
                self.add(sec, ec, self.ec[ec]["spec"], f"included; mart = independent = {n}",
                         f"included = {eid in ids}; mart = {got}", eid in ids and got == n)
            else:
                later = next(x for x in range(d + 1, self.days) if eid in independent(sub, x)[1])
                n2, _ = independent(sub, later)
                got2 = mart_value(sub, later)
                self.add(sec, ec, self.ec[ec]["spec"],
                         f"excluded at {self.day(d)}; included from {self.day(later)}",
                         f"at {self.day(d)}: excluded = {eid not in ids}, mart {got} vs {n}; at {self.day(later)}: mart {got2} vs {n2}",
                         eid not in ids and got == n and got2 == n2)

        lag = (raw["ingested_at"] - raw["event_time"]).dt.total_seconds() / 86_400
        p99 = float(np.percentile(lag, 99))
        self.add(sec, "C-BAS-03 analog", "Late-arrival p99 within A-OPS-03", f"<= {self.window} days", f"{p99:.3f} days", p99 <= self.window)

    # ----------------------------------------------- point in time / labels
    def point_in_time(self):
        sec = "Point-in-time and labels"
        feats, raw = self.d["features.churn_features"], self.d["raw.subscription_events"]
        truth = self.truth.set_index("subscriber_id")
        pos = feats[feats["label_churned_30d"] == True]  # noqa: E712
        cancels = raw[raw["event_type"] == "subscription_cancelled"].drop_duplicates("event_id")
        cancel_day = cancels.set_index("subscriber_id")["event_time"].dt.tz_convert(self.zone).dt.date
        wrong = [r for r in pos.itertuples() if not (r.feature_date < cancel_day.get(r.subscriber_id, r.feature_date) <= r.feature_date + timedelta(days=30))]
        self.add(sec, "EC-14", self.ec["EC-14"]["spec"], "every positive label has a cancellation in (d, d+30]",
                 f"{len(pos)} positives, {len(wrong)} inconsistent", len(pos) > 0 and not wrong)
        leak = feats.merge(cancel_day.rename("cancel_date"), left_on="subscriber_id", right_index=True)
        leak = leak[leak["cancel_date"] <= leak["feature_date"]]
        self.add(sec, "EC-14 (rows)", "No feature row on or after its subscriber's cancellation date", "0 rows", f"{len(leak)} rows", leak.empty)
        inv = set(truth.index[truth["involuntary"]])
        inv_pos = pos[pos["subscriber_id"].isin(inv)]
        self.add(sec, "EC-15", self.ec["EC-15"]["spec"], "> 0 dunning churners labeled true", f"{inv_pos['subscriber_id'].nunique()} subscribers",
                 inv_pos["subscriber_id"].nunique() > 0)
        unmatured = feats[feats["feature_date"] > self.end - timedelta(days=31)]
        self.add(sec, "Label maturity", "Labels are null until 30 days have passed", "all null for recent dates",
                 f"{int(unmatured['label_churned_30d'].notna().sum())} non-null", unmatured["label_churned_30d"].isna().all())

    # -------------------------------------------------------- MLOps/RAGOps
    def mlops(self):
        sec = "MLOps / RAGOps code paths"
        rep = self.m["model_report"]
        kpis = metrics.churn_model_kpis(rep["y_test"], rep["p_test"])
        handoff = yaml.safe_load((ROOT / "mlops" / "churn_handoff.yaml").read_text(encoding="utf-8"))
        fails = release_gate(kpis, handoff["go_live_thresholds"])
        self.add(sec, "Churn go-live gate", "KPIs compute end to end and the gate evaluates (H-09)",
                 "gate runs", f"AUC {kpis['roc_auc']:.3f}, lift {kpis['lift_top_decile']:.2f}, ECE {kpis['ece']:.3f}; "
                 f"failures: {fails or 'none'} (test {rep['test_date']}, n={rep['test_rows']}, positives={rep['test_positives']})",
                 True, required=False)

        feats = self.d["features.churn_features"]
        dates = sorted(feats["feature_date"].unique())
        ref, cur = feats[feats["feature_date"] == dates[0]], feats[feats["feature_date"] == dates[-1]]
        cols = ["tenure_days", "sessions_30d", "email_open_rate_30d", "orders_90d"]
        res = drift.feature_drift({c: ref[c].dropna().astype(float) for c in cols}, {c: cur[c].dropna().astype(float) for c in cols})
        shifted = drift.psi(ref["sessions_30d"].astype(float), ref["sessions_30d"].astype(float) + 10)
        self.add(sec, "Drift (H-16)", "PSI computes on real feature columns; an injected shift is caught",
                 "shift -> act", f"{', '.join(f'{r.signal.split(':')[1]} {r.value:.3f} {r.status}' for r in res)}; shifted {shifted:.2f} {drift.psi_status(shifted)}",
                 drift.psi_status(shifted) == "act")

        rag = self.d["rag.transcript_chunks"]
        published = rag.loc[self.outcomes["rag.transcript_chunks"].published.index] if self.outcomes["rag.transcript_chunks"].publish else \
            rag[~rag.index.isin(self.outcomes["rag.transcript_chunks"].quarantined.index)]
        texts = published["chunk_text_redacted"].tolist()
        vec = TfidfVectorizer(token_pattern=r"[A-Za-z0-9-]+").fit(texts)
        mat = vec.transform(texts)
        orders = published["chunk_text_redacted"].str.extract(r"(ORD-\d{6})")[0]
        runs = []
        for order in orders.dropna().unique()[:100]:
            gold = set(published.loc[orders == order, "chunk_id"])
            q = vec.transform([f"What happened with order {order}?"])
            top = np.argsort(-linear_kernel(q, mat).ravel())[:10]
            runs.append((published["chunk_id"].iloc[top].tolist(), gold))
        rep_r = metrics.retrieval_report(runs, k=5) if runs else {}
        rag_thr = yaml.safe_load((ROOT / "mlops" / "ragops_plan.yaml").read_text(encoding="utf-8"))["evaluation"]["release_thresholds"]
        gate = release_gate(rep_r, {k: v for k, v in rag_thr.items() if k in ("recall@5", "mrr@10", "ndcg@10")})
        self.add(sec, "RAG eval harness", "Golden-set retrieval metrics compute and the release gate evaluates (TF-IDF stand-in retriever)",
                 "harness runs", f"{len(runs)} questions; " + ", ".join(f"{k} {v:.3f}" for k, v in rep_r.items()) + f"; gate failures: {gate or 'none'}",
                 bool(runs), required=False)
        over = int((rag["token_count"] > self.a["A-TRN-04"]["base"]).sum())
        self.add(sec, "Chunk size", "No chunk exceeds A-TRN-04", f"<= {self.a['A-TRN-04']['base']} tokens", f"{over} over", over == 0)

    # ---------------------------------------------------------- calibration
    def calibration(self):
        sec = "Calibration to assumptions (base)"
        raw, subs = self.d["raw.subscription_events"], self.src["subscription_platform.subscribers"]
        truth = self.truth
        start = truth["start_day"].clip(lower=0)
        end = truth["cancel_day"].where(truth["cancel_day"] >= 0, self.days - 1)
        sub_days = int((end - start + 1).clip(lower=0).sum())
        n_sub_events = int(raw["subscriber_id"].notna().sum())
        expected = self.a["A-EVT-01"]["base"] * self.a["A-EVT-02"]["base"] + self.a["A-EVT-03"]["base"]
        actual = n_sub_events / sub_days
        self.add(sec, "Events / subscriber-day", "A-EVT-01 x A-EVT-02 + A-EVT-03", f"{expected:.1f} +/- 25%", f"{actual:.2f}",
                 abs(actual / expected - 1) <= 0.25, required=False)
        churned = int((truth["cancel_day"] >= 0).sum())
        monthly = churned / (sub_days / 30.4)
        self.add(sec, "Monthly churn", "A-GRW-04", f"{self.a['A-GRW-04']['base']:.0%} +/- 40%", f"{monthly:.1%}",
                 abs(monthly / self.a["A-GRW-04"]["base"] - 1) <= 0.4, required=False)
        tickets = self.src["helpdesk.tickets"]
        rate = len(tickets) / (sub_days / 30.4)
        self.add(sec, "Contact rate", "A-TRN-01", f"{self.a['A-TRN-01']['base']:.2f} +/- 50%", f"{rate:.3f} per subscriber-month",
                 abs(rate / self.a["A-TRN-01"]["base"] - 1) <= 0.5, required=False)
        local = raw["event_time"].dt.tz_convert(self.zone).dt.date
        per_day = local.value_counts()
        promo = per_day.get(self.day(self.m["promo_day"]), 0)
        ratio = promo / per_day.median()
        self.add(sec, "Promo-day peak", "A-EVT-04", f"~{self.a['A-EVT-04']['base']:.1f}x median day (sessions only scaled)",
                 f"{ratio:.2f}x", ratio > 1.5, required=False)

    # ------------------------------------------------------------- lineage
    def lineage(self) -> dict:
        sec = "Lineage coverage"
        lin = yaml.safe_load(LINEAGE.read_text(encoding="utf-8"))
        problems = []
        for name, ds in self.contracts.datasets.items():
            cols = lin["datasets"].get(name, {}).get("columns", {})
            missing = [c.name for c in ds.columns if c.name not in cols]
            extra = [c for c in cols if c not in {x.name for x in ds.columns}]
            problems += [f"{name}.{c} missing" for c in missing] + [f"{name}.{c} not in contract" for c in extra]
            for col, spec in cols.items():
                for ref in spec["sources"]:
                    if ref == "system":
                        continue
                    table, field = ref.rsplit(".", 1)
                    if table in self.src:
                        ok = field in self.src[table].columns
                    elif table in self.contracts.datasets:
                        ok = field in {c.name for c in self.contracts.datasets[table].columns}
                    else:
                        ok = False
                    if not ok:
                        problems.append(f"{name}.{col}: unknown source {ref}")
        n = sum(len(ds.columns) for ds in self.contracts.datasets.values())
        self.add(sec, "lineage.yaml", "Every contract column has lineage to an existing source field", f"{n} columns mapped",
                 "; ".join(problems) or f"{n} columns mapped", not problems)
        return lin

    # -------------------------------------------------------------- render
    def render(self, lin: dict) -> None:
        df = pd.DataFrame([vars(c) for c in self.checks])
        failed_req = df[(~df["passed"]) & df["required"]]
        summary = df.groupby("section", sort=False).agg(checks=("id", "size"), passed=("passed", "sum"),
                                                        required=("required", "sum")).reset_index()
        ec_rows = pd.DataFrame([{"ID": c["id"], "Edge case": c["title"], "Dataset": c["dataset"], "Rule": c["rule"] or "spec check",
                                 "Expected": c["expected_behavior"], "Spec": c["spec"]} for c in self.m["edge_cases"]])
        sections = []
        for sec, g in df.groupby("section", sort=False):
            t = g.assign(result=g.apply(lambda r: ("pass" if r.passed else "**FAIL**") + ("" if r.required else " (info)"), axis=1))
            sections.append(f"### {sec}\n\n" + t[["id", "spec", "expected", "actual", "result"]].rename(columns=str.title).to_markdown(index=False))
        counts = pd.DataFrame([{"Table": k, "Rows": f"{v:,}"} for k, v in self.m["row_counts"].items()])
        cfg = self.m["config"]
        self.report.write_text(f"""# Synthetic Data Validation Report

_Generated by `scripts/validate_synthetic.py` from `synthetic/data/` (produced by
`scripts/generate_synthetic.py`). Do not edit by hand._

**Result: {'PASS' if failed_req.empty else 'FAIL'}** — {int(df['passed'].sum())}/{len(df)} checks passed
({len(failed_req)} required failures). Generated {cfg['subscribers']} subscribers x {cfg['days']} days,
seed {cfg['seed']}, build at {self.m['build_time']}. Behavior parameters come from the
assumptions register (base scenario); all personal data is fictitious.

Required checks validate the specs (contracts, governance rules, ADR-003 incremental
semantics, security matrix controls, lineage). Informational checks exercise the
MLOps/RAGOps code paths and show calibration to the assumptions; synthetic model and
retrieval scores say nothing about real-world quality.

## Summary

{summary.rename(columns=str.title).to_markdown(index=False)}

## Seeded edge cases

{ec_rows.to_markdown(index=False)}

## Results

{(chr(10) * 2).join(sections)}

## Generated tables

{counts.to_markdown(index=False)}
""", encoding="utf-8")

        blocks = []
        for name, spec in lin["datasets"].items():
            rows = pd.DataFrame([{"Column": c, "Sources": ", ".join(f"`{s}`" for s in v["sources"]), "Transformation": v["transform"]}
                                 for c, v in spec["columns"].items()])
            note = f"\n\n{' '.join(spec['notes'].split())}" if spec.get("notes") else ""
            blocks.append(f"### `{name}`\n\n**Produced by:** `{spec['produced_by']}` · **Synthetic implementation:** "
                          f"`{spec['implemented_in']}`{note}\n\n{rows.to_markdown(index=False)}")
        systems = pd.DataFrame([{"Source table": f"`{k}`", "System": v} for k, v in lin["source_systems"].items()])
        edges = set()
        for name, spec in lin["datasets"].items():
            for v in spec["columns"].values():
                for ref in v["sources"]:
                    if ref != "system":
                        up = ref.rsplit(".", 1)[0]
                        if up != name:
                            edges.add((up, name))
        node = lambda s: s.replace(".", "_")  # noqa: E731
        mermaid = "\n".join(f"    {node(u)}[\"{u}\"] --> {node(v)}[\"{v}\"]" for u, v in sorted(edges))
        self.lineage_md.write_text(f"""# Data Lineage — Subscription Health Data Product

_Generated by `scripts/validate_synthetic.py` from `synthetic/lineage.yaml`. Do not edit by hand.
Coverage is checked against every contract column and every source-table field._

## Dataset lineage

```mermaid
flowchart LR
{mermaid}
```

## Source systems

{systems.to_markdown(index=False)}

## Field lineage

{(chr(10) * 2).join(blocks)}
""", encoding="utf-8")

    def run(self) -> int:
        self.contracts_and_edge_cases()
        self.column_tests()
        self.scenarios()
        self.incremental()
        self.point_in_time()
        self.mlops()
        self.calibration()
        lin = self.lineage()
        self.render(lin)
        failed = [c for c in self.checks if c.required and not c.passed]
        info = [c for c in self.checks if not c.required]
        print(f"{len(self.checks) - len(failed) - len(info)}/{len(self.checks) - len(info)} required checks passed; "
              f"{sum(c.passed for c in info)}/{len(info)} informational")
        for c in failed:
            print(f"  FAIL [{c.section}] {c.id}: expected {c.expected}; got {c.actual}", file=sys.stderr)
        print(f"Wrote {self.report.name} and {self.lineage_md.name}")
        return 1 if failed else 0


def _prefix(dataset: str) -> str:
    return {"raw.subscription_events": "GR-EVT", "marts.retention_subscription_health_daily": "GR-RET",
            "marts.retention_events_hourly": "GR-HRL", "features.churn_features": "GR-FEA",
            "rag.transcript_chunks": "GR-RAG"}[dataset]


if __name__ == "__main__":
    if not (DATA / "manifest.json").exists():
        sys.exit("No synthetic data found; run scripts/generate_synthetic.py first.")
    sys.exit(Validator().run())
