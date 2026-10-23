"""Governance rules: each rule's pass case and failure behavior on synthetic batches."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from aurora_contracts.governance import CheckContext, asset_check_severity, evaluate
from aurora_contracts.models import FailureBehavior

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2027, 3, 10, 8, 0, tzinfo=TZ).astimezone(timezone.utc)  # after the 07:00 deadline
YESTERDAY = date(2027, 3, 9)


def consent_table(ids, **overrides) -> pd.DataFrame:
    base = {"analytics_objection": False, "deletion_pending": False, "ai_processing": True}
    df = pd.DataFrame([{**base, "subscriber_id": i} for i in ids], columns=[*base, "subscriber_id"])
    df = df.set_index("subscriber_id")
    for attr, values in overrides.items():
        for sid, v in values.items():
            df.loc[sid, attr] = v
    return df


def features_batch(n: int = 200, feature_date: date = YESTERDAY) -> pd.DataFrame:
    ids = [f"sub_{i:05d}" for i in range(n)]
    return pd.DataFrame(
        {
            "subscriber_id": ids,
            "feature_date": [feature_date] * n,
            "tenure_days": [100 + i for i in range(n)],
            "plan_interval": ["monthly"] * n,
            "subscription_status": ["active"] * n,
            "orders_90d": [3] * n,
            "skips_90d": [0] * n,
            "pauses_180d": [0] * n,
            "failed_payments_30d": [0] * n,
            "sessions_30d": [4] * n,
            "cancel_page_views_14d": [0] * n,
            "email_open_rate_30d": [0.4] * n,
            "support_contacts_90d": [1] * n,
            "days_since_last_order": [20] * n,
            "label_churned_30d": [None] * n,
            "feature_version": ["1.0.0"] * n,
            "_loaded_at": [pd.Timestamp(NOW - timedelta(hours=3))] * n,
        }
    )


def chunks_batch(texts: list[str]) -> pd.DataFrame:
    n = len(texts)
    return pd.DataFrame(
        {
            "chunk_id": [f"c{i}" for i in range(n)],
            "ticket_id": [f"t{i}" for i in range(n)],
            "subscriber_id": [f"sub_{i:05d}" for i in range(n)],
            "chunk_index": [0] * n,
            "chunk_text_redacted": texts,
            "token_count": [120] * n,
            "topic_tags": [["billing"]] * n,
            "ticket_closed_at": [pd.Timestamp(NOW - timedelta(hours=20))] * n,
            "redaction_version": ["r1"] * n,
            "_loaded_at": [pd.Timestamp(NOW - timedelta(hours=2))] * n,
        }
    )


def events_batch(n: int = 50, ingested_ago: timedelta = timedelta(minutes=5)) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event_id": [f"e{i}" for i in range(n)],
            "event_type": ["page_view"] * n,
            "source": ["web"] * n,
            "event_time": [pd.Timestamp(NOW - timedelta(minutes=20))] * n,
            "ingested_at": [pd.Timestamp(NOW - ingested_ago)] * n,
            "subscriber_id": [None] * n,
            "anonymous_id": ["anon"] * n,
            "ip_address": ["203.0.113.7"] * n,
            "user_agent": ["Mozilla/5.0"] * n,
            "properties": [{"path": "/plans"}] * n,
        }
    )


def ctx(ids=(), gates=frozenset({"SEC-G3"}), misses=None, **consent_overrides) -> CheckContext:
    return CheckContext(
        now=NOW,
        local_tz=TZ,
        consent=consent_table(ids, **consent_overrides),
        gates_cleared=frozenset(gates),
        consecutive_failures=misses or {},
    )


@pytest.fixture
def features(layer):
    return layer.datasets["features.churn_features"]


@pytest.fixture
def chunks(layer):
    return layer.datasets["rag.transcript_chunks"]


@pytest.fixture
def events(layer):
    return layer.datasets["raw.subscription_events"]


# ------------------------------------------------------------ clean batches
def test_clean_feature_batch_publishes_everything(features):
    df = features_batch()
    out = evaluate(df, features, ctx(df["subscriber_id"]))
    assert out.publish and not out.failed
    assert len(out.published) == len(df) and out.quarantined.empty


# --------------------------------------------------------------- null / PII
def test_null_in_required_feature_blocks_publish(features):
    df = features_batch()
    df.loc[3, "tenure_days"] = None
    out = evaluate(df, features, ctx(df["subscriber_id"]))
    assert not out.publish and out.published.empty
    assert [r.rule_id for r in out.failed] == ["GR-FEA-02"]
    assert out.failed[0].behavior is FailureBehavior.BLOCK_PUBLISH


def test_null_in_raw_event_is_quarantined_not_dropped(events):
    df = events_batch()
    df.loc[0, "event_type"] = None
    out = evaluate(df, events, ctx())
    assert out.publish and len(out.quarantined) == 1
    assert out.quarantined["_quarantine_reason"].iloc[0] == "GR-EVT-02"


def test_residual_email_in_chunk_is_quarantined(chunks):
    df = chunks_batch(["Customer asked about a refund.", "Reach me at jane.doe@example.com please"])
    out = evaluate(df, chunks, ctx(df["subscriber_id"]))
    assert out.publish
    assert list(out.quarantined["chunk_id"]) == ["c1"] and list(out.published["chunk_id"]) == ["c0"]


def test_card_number_in_chunk_opens_security_incident(chunks):
    df = chunks_batch(["My card is 4111 1111 1111 1111 and it was declined", "Shipping was late."])
    out = evaluate(df, chunks, ctx(df["subscriber_id"]))
    assert not out.publish and out.incidents
    assert out.failed[0].behavior is FailureBehavior.SECURITY_INCIDENT
    assert list(out.quarantined["chunk_id"]) == ["c0"]


def test_non_luhn_digit_run_is_not_a_card_number(chunks):
    df = chunks_batch(["Order number 1234 5678 9012 3456 arrived damaged"])
    out = evaluate(df, chunks, ctx(df["subscriber_id"]))
    assert out.publish and out.quarantined.empty


# ---------------------------------------------------------------- freshness
def test_late_daily_partition_serves_stale_then_blocks(features):
    df = features_batch(feature_date=YESTERDAY - timedelta(days=1))
    first = evaluate(df, features, ctx(df["subscriber_id"]))
    assert first.publish and first.stale_banner
    assert first.failed[0].behavior is FailureBehavior.SERVE_STALE_AND_ALERT

    second = evaluate(df, features, ctx(df["subscriber_id"], misses={"GR-FEA-04": 1}))
    assert not second.publish
    assert second.failed[0].behavior is FailureBehavior.BLOCK_PUBLISH


def test_before_deadline_previous_partition_is_still_fresh(features):
    df = features_batch(feature_date=YESTERDAY - timedelta(days=1))
    early = CheckContext(
        now=datetime(2027, 3, 10, 6, 0, tzinfo=TZ).astimezone(timezone.utc),
        local_tz=TZ,
        consent=consent_table(df["subscriber_id"]),
    )
    assert evaluate(df, features, early).publish


def test_landing_delay_warns_then_blocks(events):
    df = events_batch(ingested_ago=timedelta(minutes=45))
    out = evaluate(df, events, ctx())
    assert out.publish and out.failed[0].behavior is FailureBehavior.WARN
    out = evaluate(df, events, ctx(misses={"GR-EVT-05": 3}))
    assert not out.publish


# ------------------------------------------------------------ consent scope
def test_objecting_subscriber_is_quarantined_from_features(features):
    df = features_batch()
    out = evaluate(df, features, ctx(df["subscriber_id"], analytics_objection={"sub_00007": True}))
    assert out.publish
    assert list(out.quarantined["subscriber_id"]) == ["sub_00007"]
    assert "sub_00007" not in set(out.published["subscriber_id"])


def test_consent_failures_above_limit_block_publish(features):
    df = features_batch()
    pending = {f"sub_{i:05d}": True for i in range(5)}  # 2.5% > 1% limit
    out = evaluate(df, features, ctx(df["subscriber_id"], deletion_pending=pending))
    assert not out.publish
    assert out.failed[0].behavior is FailureBehavior.BLOCK_PUBLISH


def test_unknown_consent_state_fails_closed(features):
    df = features_batch()
    known = df["subscriber_id"].iloc[1:]  # first subscriber has no consent record
    out = evaluate(df, features, ctx(known))
    assert list(out.quarantined["subscriber_id"]) == ["sub_00000"]


def test_rag_blocked_until_sec_g3_cleared(chunks):
    df = chunks_batch(["Customer asked about pausing."])
    out = evaluate(df, chunks, ctx(df["subscriber_id"], gates=frozenset()))
    assert not out.publish and "SEC-G3" in out.failed[0].message


# ---------------------------------------------------------- schema / unique
def test_schema_drift_blocks_and_skips_row_checks(events):
    df = events_batch().assign(email="x@example.com")
    out = evaluate(df, events, ctx())
    assert not out.publish
    assert [r.rule_id for r in out.results] == ["GR-EVT-01"]


def test_duplicate_event_ids_keep_first(events):
    df = pd.concat([events_batch(3), events_batch(1)], ignore_index=True)
    out = evaluate(df, events, ctx())
    assert out.publish
    assert len(out.quarantined) == 1 and out.published["event_id"].is_unique


@pytest.mark.parametrize(
    ("behavior", "severity"),
    [(FailureBehavior.BLOCK_PUBLISH, "ERROR"), (FailureBehavior.SECURITY_INCIDENT, "ERROR"),
     (FailureBehavior.QUARANTINE_ROWS, "WARN"), (FailureBehavior.WARN, "WARN")],
)
def test_asset_check_severity(behavior, severity):
    assert asset_check_severity(behavior) == severity
