"""Static conformance of the contract layer to ADR-001..003, the security
matrix and the assumptions register. Every check yields a Finding; any failed
Finding fails the build."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .loader import ContractLayer
from .models import CLASSIFICATION_ORDER, ConsentScopeRule, FailureBehavior, FreshnessRule, UniqueRule

MICRO_BATCH_LANDING_MINUTES = 15  # ADR-003
HOURLY_BUILD_MINUTES = 60  # ADR-003


@dataclass(frozen=True)
class Finding:
    check: str
    basis: str
    subject: str
    passed: bool
    detail: str


def _rank(level: str) -> int:
    return CLASSIFICATION_ORDER.index(level)


def check(layer: ContractLayer) -> list[Finding]:
    f: list[Finding] = []

    def add(check_id: str, basis: str, subject: str, ok: bool, detail: str) -> None:
        f.append(Finding(check_id, basis, subject, ok, detail))

    sec = layer.security
    a = layer.assumptions
    inventory = {e["id"]: e for e in sec["pii_inventory"]}
    rules = {r["id"] for r in sec["retention_rules"]}
    rbac = {r["role"]: r for r in sec["rbac_roles"]}
    purposes = {p["id"]: p for p in sec["consent_purposes"]}
    gates = {g["id"] for g in sec["review_gates"]}
    sla_deadline = time.fromisoformat(a["A-OPS-02"]["base"])
    lookback = a["A-OPS-03"]["base"]
    chunk_tokens = a["A-TRN-04"]["base"]

    for ds in layer.datasets.values():
        n = ds.name
        consumers = layer.consumers_of(n)

        # ---------------------------------------------------------- ADR-001
        add("C01", "ADR-001", n, ds.platform == "warehouse", "lives in the single managed warehouse")
        logic_ok = ds.produced_by.startswith("dbt:") or (ds.layer == "raw" and ds.produced_by.startswith("collector:"))
        add("C02", "ADR-001", n, logic_ok, f"produced by {ds.produced_by} (dbt for all transformation logic)")
        if ds.layer == "features":
            add("C03", "ADR-001", n, ds.open_format_export, "feature table published with an open-format export")
        add("C04", "ADR-001", n, all(c.pii == (c.pii_element is not None) for c in ds.columns),
            "every column carries a pii tag consistent with the PII inventory")

        # ---------------------------------------------------------- ADR-002
        add("C05", "ADR-002", n, bool(ds.owner), f"owner: {ds.owner}")
        kinds = {r.rule for r in ds.governance}
        add("C06", "ADR-002", n, {"schema", "not_null", "freshness"} <= kinds,
            f"has schema, not-null and freshness checks (has: {sorted(kinds)})")
        add("C07", "ADR-002", n, any(r.on_fail.blocking or getattr(r, "escalate_to", FailureBehavior.WARN).blocking
                                     for r in ds.governance),
            "at least one check can block publish (blocking asset check)")

        # ---------------------------------------------------------- ADR-003
        for r in ds.governance:
            if isinstance(r, FreshnessRule) and r.max_age_minutes is not None:
                floor = MICRO_BATCH_LANDING_MINUTES if ds.layer == "raw" else HOURLY_BUILD_MINUTES
                add("C08", "ADR-003", f"{n} {r.id}", r.max_age_minutes >= floor,
                    f"max age {r.max_age_minutes} min is achievable with micro-batch (>= {floor} min)")
            if isinstance(r, FreshnessRule) and r.deadline_local is not None:
                add("C09", "A-OPS-02", f"{n} {r.id}", r.deadline_local == sla_deadline,
                    f"daily deadline {r.deadline_local} matches A-OPS-02 ({sla_deadline})")
        if ds.materialization == "incremental":
            add("C10", "ADR-003 / A-OPS-03", n, ds.incremental_lookback_days == lookback,
                f"incremental lookback {ds.incremental_lookback_days} d equals late-arrival window {lookback} d")
        if ds.layer == "raw":
            cols = {c.name: c for c in ds.columns}
            ok = all(k in cols and not cols[k].nullable for k in ("event_id", "event_time", "ingested_at"))
            ok &= any(isinstance(r, UniqueRule) and r.columns == ["event_id"] for r in ds.governance)
            add("C11", "ADR-003", n, ok, "event contract: non-null event_id (unique), event_time, ingested_at")

        # ------------------------------------------------- security matrix
        add("C12", "Security matrix", n, ds.retention_rule in rules, f"retention rule {ds.retention_rule} exists")
        add("C13", "Security matrix", n, all(c.classification != "C4" for c in ds.columns),
            "no Prohibited (C4) columns")
        for c in ds.columns:
            if c.pii_element:
                el = inventory.get(c.pii_element)
                ok = el is not None and _rank(c.classification) >= _rank(el["classification"])
                add("C14", "Security matrix", f"{n}.{c.name}", ok,
                    f"classified {c.classification}, not below inventory {c.pii_element} "
                    f"({el['classification'] if el else 'missing'})")
        if any(c.health_tagged for c in ds.columns):
            add("C15", "Security matrix (ABAC-03)", n,
                all("P7" in con.purposes for con in consumers), "health-tagged columns only for P7 consumers")
        for r in ds.governance:
            if isinstance(r, ConsentScopeRule):
                p = purposes.get(r.purpose)
                attr = p["enforcement_attribute"].removeprefix("consent.") if p else None
                add("C16", "Security matrix (consent)", f"{n} {r.id}", p is not None and attr in r.require,
                    f"consent rule enforces {p['enforcement_attribute'] if p else '?'} for {r.purpose}")
                add("C17", "Security matrix (ABAC-06)", f"{n} {r.id}", r.require.get("deletion_pending") is False,
                    "deletion-pending subscribers are excluded")
                add("C18", "Security matrix (gates)", f"{n} {r.id}", set(r.required_gates) <= gates,
                    f"required gates {r.required_gates} exist")
        if ds.layer == "rag":
            col = next((c for c in ds.columns if c.name == "token_count"), None)
            add("C19", "A-TRN-04", n, col is not None and col.max_value == chunk_tokens,
                f"chunk token cap equals A-TRN-04 ({chunk_tokens})")
        consent_purposes = {r.purpose for r in ds.governance if isinstance(r, ConsentScopeRule)}
        if any(c.classification in ("C2", "C3") and c.name == "subscriber_id" for c in ds.columns):
            add("C20", "Security matrix (consent)", n,
                all(set(con.purposes) <= consent_purposes for con in consumers),
                "subscriber-level data enforces every consumer purpose with a consent-scope rule")

    # ------------------------------------------------------------ consumers
    for con in layer.register.consumers:
        for d in con.datasets:
            ds = layer.datasets.get(d)
            add("C21", "Register", f"{con.id} -> {d}", ds is not None, "dataset has a contract")
            if ds is None:
                continue
            add("C22", "Security matrix (classification)", f"{con.id} -> {d}",
                _rank(ds.classification) <= _rank(con.max_classification),
                f"dataset {ds.classification} within consumer max {con.max_classification}")
            for role in con.roles:
                grant = rbac.get(role, {}).get(ds.classification, "none")
                add("C23", "Security matrix (RBAC)", f"{con.id} {role} -> {d}",
                    role in rbac and not grant.startswith(("none", "synthetic")),
                    f"role grant on {ds.classification}: {grant}")
        for p in con.purposes:
            add("C24", "Security matrix (consent)", f"{con.id} {p}", p in purposes, f"purpose {p} exists")
        if "P8" in con.purposes:
            add("C25", "ADR-004 / SEC-G3", con.id, "SEC-G3" in con.required_gates,
                "AI-processing consumer is gated by SEC-G3")
        for g in con.required_gates:
            add("C26", "Security matrix (gates)", f"{con.id} {g}", g in gates, f"gate {g} exists")
        if con.sla.daily_deadline_local:
            add("C27", "A-OPS-02", con.id, con.sla.daily_deadline_local == sla_deadline,
                f"SLA deadline {con.sla.daily_deadline_local} matches A-OPS-02")
        if con.sla.max_lag_minutes is not None:
            add("C28", "ADR-003", con.id, con.sla.max_lag_minutes >= HOURLY_BUILD_MINUTES,
                f"intraday lag SLA {con.sla.max_lag_minutes} min is achievable without streaming")
    return f
