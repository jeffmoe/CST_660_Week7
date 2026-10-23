"""Seeded edge cases. Each one is injected into the simulated source systems
(never patched into an output table), so it flows through the same
transformations as normal data, and the manifest records what the specs say
must happen to it."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from .simulate import World


@dataclass
class EdgeCase:
    id: str
    title: str
    spec: str  # what the specs say must happen
    dataset: str
    rule: str | None  # governance rule expected to fire, or None for spec checks
    expected_behavior: str  # failure behavior, or "pass" / a spec assertion
    keys: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _stable_subjects(world: World, k: int) -> list[str]:
    """Long-tenured subscribers active through the end of the window, low churn propensity."""
    s = world.subscribers
    pool = s[(s["start_day"] < 0) & (s["cancel_day"] < 0)].sort_values("propensity")
    return pool["subscriber_id"].head(k).tolist()


def _add_ticket(world: World, sid: str, topic: str, text: str, day_offset: int = 2) -> str:
    cfg = world.cfg
    tid = f"tkt_ec_{len(world.tickets):04d}"
    opened = datetime.combine(cfg.end_date - timedelta(days=day_offset), time(10), cfg.zone)
    row = {"ticket_id": tid, "subscriber_id": sid, "topic": topic, "day": cfg.days - day_offset,
           "opened_at": opened, "transcript_raw": text, "closed_at": opened + timedelta(hours=2)}
    world.tickets = pd.concat([world.tickets, pd.DataFrame([row])], ignore_index=True)
    return tid


def seed(world: World) -> list[EdgeCase]:
    cfg, ev = world.cfg, world.events
    rng = np.random.default_rng(cfg.seed + 1)
    subj = _stable_subjects(world, 7)
    objector, deleting, no_ai, unknown, pii_sub, card_sub, late_sub = subj
    sub = world.subscribers.set_index("subscriber_id")
    cases: list[EdgeCase] = []

    # EC-01 consent-scope violation: subscriber objected to analytics (P5).
    world.consent.loc[world.consent["subscriber_id"] == objector, "analytics_objection"] = True
    cases.append(EdgeCase("EC-01", "Consent-scope violation: analytics objection",
                          "Objecting subscriber is quarantined from the retention mart and churn features (ABAC-02)",
                          "marts.retention_subscription_health_daily, features.churn_features", "GR-RET-05, GR-FEA-05",
                          "quarantine_rows", {"subscriber_id": objector}))

    # EC-02 PII in transcript: customer gives a second email the profile-based redactor does not know.
    s = sub.loc[pii_sub]
    tid = _add_ticket(world, pii_sub, "billing", "\n".join([
        f"Customer: Hi, this is {s.first_name} {s.last_name}. Please send receipts to my work address too.",
        f"Customer: It's {s.first_name.lower()}.{s.last_name.lower()}@contoso-mail.example.org.",
        "Agent: Done, receipts will go to both addresses from now on.",
    ]))
    cases.append(EdgeCase("EC-02", "PII in transcript: secondary email missed by redaction",
                          "Residual email in a redacted chunk is withheld from the index (GR-RAG-03, C-AI-02)",
                          "rag.transcript_chunks", "GR-RAG-03", "quarantine_rows", {"ticket_id": tid}))

    # EC-03 Prohibited data: card number typed without separators (redactor r1 only handles grouped format).
    s = sub.loc[card_sub]
    tid = _add_ticket(world, card_sub, "billing", "\n".join([
        f"Customer: My card keeps failing. The number is 4111111111111111 if that helps, {s.first_name} here.",
        "Agent: Please don't share card numbers here; I'll send a secure link to update payment details.",
    ]))
    cases.append(EdgeCase("EC-03", "Card number in transcript",
                          "Card number in a chunk opens a security incident and blocks the batch (R-04, ABAC-07)",
                          "rag.transcript_chunks", "GR-RAG-03", "security_incident", {"ticket_id": tid}))

    # EC-04 deletion request pending: excluded everywhere (ABAC-06).
    world.consent.loc[world.consent["subscriber_id"] == deleting, "deletion_pending"] = True
    tid = _add_ticket(world, deleting, "cancellation", "Customer: Please delete my account and data.\n"
                      "Agent: I've submitted your deletion request; it completes within 45 days.", day_offset=3)
    cases.append(EdgeCase("EC-04", "Deletion request pending",
                          "Deletion-pending subscriber is excluded from mart, features and RAG chunks (ABAC-06, C-SEC-03)",
                          "marts.retention_subscription_health_daily, features.churn_features, rag.transcript_chunks",
                          "GR-RET-05, GR-FEA-05, GR-RAG-05", "quarantine_rows", {"subscriber_id": deleting, "ticket_id": tid}))

    # EC-10 no AI-processing consent (P8): chunks withheld from the assistant.
    world.consent.loc[world.consent["subscriber_id"] == no_ai, "ai_processing"] = False
    tid = _add_ticket(world, no_ai, "shipping", "Customer: My box is late again.\n"
                      "Agent: Sorry about that, it's out for delivery today.", day_offset=4)
    cases.append(EdgeCase("EC-10", "No AI-processing consent",
                          "Subscriber outside P8 scope never reaches the RAG index (GR-RAG-05)",
                          "rag.transcript_chunks", "GR-RAG-05", "quarantine_rows", {"subscriber_id": no_ai, "ticket_id": tid}))

    # EC-13 consent state unknown: subscriber missing from the ledger -> fail closed.
    world.consent = world.consent[world.consent["subscriber_id"] != unknown].reset_index(drop=True)
    tid = _add_ticket(world, unknown, "product", "Customer: Can I switch flavors?\nAgent: Yes, from your account page.",
                      day_offset=5)
    cases.append(EdgeCase("EC-13", "Consent state unknown",
                          "Missing consent record fails closed in every consent-scoped dataset",
                          "marts.retention_subscription_health_daily, features.churn_features, rag.transcript_chunks",
                          "GR-RET-05, GR-FEA-05, GR-RAG-05", "quarantine_rows", {"subscriber_id": unknown, "ticket_id": tid}))

    # ----------------------------------------------------------- event stream
    def pick(etype: str, sid: str | None = None, day: int | None = None) -> int:
        m = ev["event_type"] == etype
        if sid:
            m &= ev["subscriber_id"] == sid
        if day is not None:
            local = ev["event_time"].dt.tz_convert(cfg.zone).dt.date
            m &= local == cfg.end_date - timedelta(days=cfg.days - day)
        idx = ev.index[m]
        return int(idx[len(idx) // 2])

    # EC-05 duplicate delivery of a billing webhook.
    i = pick("payment_succeeded")
    dup = ev.loc[[i]].assign(ingested_at=ev.loc[i, "ingested_at"] + timedelta(minutes=16))
    cases.append(EdgeCase("EC-05", "Duplicate webhook delivery",
                          "Second copy of the event_id is quarantined; the merge keeps the first (GR-EVT-03)",
                          "raw.subscription_events", "GR-EVT-03", "quarantine_rows", {"event_id": ev.loc[i, "event_id"]}))

    # EC-06 / EC-07 late-arriving cancel-page visits for a churning subscriber.
    late_rows = []
    for ec, day, lag_days in (("EC-06", cfg.days - 6, 2), ("EC-07", cfg.days - 10, 5)):
        when = datetime.combine(cfg.end_date - timedelta(days=cfg.days - day), time(20), cfg.zone)
        eid = str(uuid.UUID(bytes=rng.bytes(16), version=4))
        late_rows.append({"event_id": eid, "event_type": "cancel_page_view", "source": "app",
                          "event_time": pd.Timestamp(when).tz_convert("UTC"),
                          "ingested_at": pd.Timestamp(when + timedelta(days=lag_days)).tz_convert("UTC"),
                          "subscriber_id": late_sub, "anonymous_id": "f" * 24, "ip_address": "203.0.113.9",
                          "user_agent": "AuroraApp/4.2 (Android 15)", "properties": '{"path": "/account/cancel"}'})
        within = lag_days <= cfg.late_window_days
        cases.append(EdgeCase(ec, f"Late event, {lag_days} days ({'within' if within else 'beyond'} the {cfg.late_window_days}-day window)",
                              ("Counted in its own day's partition by the incremental lookback (A-OPS-03)" if within else
                               "Partitions frozen before it arrived do not include it; later partitions do (A-OPS-03)"),
                              "marts.retention_subscription_health_daily", None,
                              "included" if within else "excluded_then_included",
                              {"event_id": eid, "subscriber_id": late_sub, "event_day": day}))

    # EC-08 null event_type from a broken producer.
    j = pick("page_view")
    bad_null = ev.loc[[j]].assign(event_id=str(uuid.UUID(bytes=rng.bytes(16), version=4)), event_type=None)
    cases.append(EdgeCase("EC-08", "Null event_type", "Event is quarantined, not dropped (GR-EVT-02)",
                          "raw.subscription_events", "GR-EVT-02", "quarantine_rows", {"event_id": bad_null["event_id"].iloc[0]}))

    # EC-09 email leaked into a free-form event payload.
    k = pick("product_view")
    leak = ev.loc[[k]].assign(event_id=str(uuid.UUID(bytes=rng.bytes(16), version=4)),
                              properties='{"path": "/search", "q": "reorder for avery.alder@example.com"}')
    cases.append(EdgeCase("EC-09", "Email in event payload", "Event with PII in properties is quarantined (GR-EVT-04)",
                          "raw.subscription_events", "GR-EVT-04", "quarantine_rows", {"event_id": leak["event_id"].iloc[0]}))

    world.events = pd.concat([ev, dup, pd.DataFrame(late_rows), bad_null, leak], ignore_index=True)

    # EC-11 is a producer payload the collector must reject; it never lands.
    cases.append(EdgeCase("EC-11", "Unknown event type at the producer",
                          "JSON Schema rejects the payload before landing (event contract v1)",
                          "raw.subscription_events (producer schema)", None, "rejected",
                          {"payload": {"event_id": str(uuid.UUID(bytes=rng.bytes(16), version=4)),
                                       "event_type": "subscription_upgraded", "source": "subscription_platform",
                                       "event_time": "2027-03-09T15:00:00Z", "subscriber_id": objector,
                                       "anonymous_id": None, "ip_address": None, "user_agent": None, "properties": {}}}))

    # Spec checks on naturally occurring data.
    cases += [
        EdgeCase("EC-12", "Promo-day small cells", "Hourly cells under 10 subscribers roll into 'suppressed' or are dropped (ABAC-08)",
                 "marts.retention_events_hourly", None, "no_cell_below_10", {"promo_day": cfg.promo_day}),
        EdgeCase("EC-14", "Point-in-time trap", "A cancellation after feature_date sets the label but changes no feature",
                 "features.churn_features", None, "label_true_features_unchanged", {}),
        EdgeCase("EC-15", "Involuntary churn", "Dunning cancellation counts as churn in the label",
                 "features.churn_features", None, "label_true", {}),
        EdgeCase("EC-16", "Health-inferable purchases", "Health-inferable SKUs exist in source orders but no health-tagged column reaches features (ABAC-03, SEC-G8)",
                 "features.churn_features", None, "no_health_columns", {}),
    ]
    world.notes["redaction_profile_subjects"] = subj
    return cases
