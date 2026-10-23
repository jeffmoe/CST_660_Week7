"""Governance rules as code: run a dataset's contract checks on a batch.

Each rule returns a RuleResult with the failure behavior to apply (after any
escalation). `evaluate` combines the results into a publish decision, the rows
to publish, the rows to quarantine, alerts and incidents. Blocking behaviors
map to blocking asset checks in the orchestrator (ADR-002); the batch is not
published and consumers keep the last good version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .models import (
    ConsentScopeRule,
    Dataset,
    DataType,
    FailureBehavior,
    FreshnessRule,
    NotNullRule,
    PiiScanRule,
    SchemaRule,
    UniqueRule,
)

PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "us_phone": re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),
    "card_number": re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    "us_ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
}

# Applied to the non-null values of a column, so a null in an integer column
# (which pandas stores as float) is the not-null rule's concern, not a type error.
_DTYPE_CHECKS = {
    DataType.STRING: lambda s: s.map(lambda v: isinstance(v, str)).all(),
    DataType.INTEGER: lambda s: pd.api.types.is_integer_dtype(s)
    or (pd.api.types.is_float_dtype(s) and (s % 1 == 0).all()),
    DataType.NUMBER: pd.api.types.is_numeric_dtype,
    DataType.BOOLEAN: lambda s: pd.api.types.is_bool_dtype(s) or s.map(type).eq(bool).all(),
    DataType.TIMESTAMP: pd.api.types.is_datetime64_any_dtype,
    DataType.DATE: lambda s: pd.api.types.is_datetime64_any_dtype(s) or s.map(lambda v: hasattr(v, "year")).all(),
    DataType.JSON: lambda s: True,
    # Parquet round-trips arrays as numpy arrays.
    DataType.ARRAY_STRING: lambda s: s.map(lambda v: isinstance(v, (list, tuple, np.ndarray))).all(),
}


@dataclass
class CheckContext:
    """Run-time inputs a check needs beyond the batch itself."""

    now: datetime
    local_tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/Chicago"))
    consent: pd.DataFrame | None = None  # indexed by subscriber_id; bool attribute columns
    gates_cleared: frozenset[str] = frozenset()
    consecutive_failures: dict[str, int] = field(default_factory=dict)  # rule id -> prior misses


@dataclass
class RuleResult:
    rule_id: str
    rule: str
    passed: bool
    behavior: FailureBehavior | None
    message: str
    failing_rows: pd.Index = field(default_factory=lambda: pd.Index([]))


@dataclass
class Outcome:
    dataset: str
    publish: bool
    published: pd.DataFrame
    quarantined: pd.DataFrame
    results: list[RuleResult]
    alerts: list[str]
    incidents: list[str]
    stale_banner: bool

    @property
    def failed(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]


def asset_check_severity(behavior: FailureBehavior) -> str:
    """Severity of the orchestrator asset check (ADR-002) for a failure behavior."""
    return "ERROR" if behavior.blocking else "WARN"


# ------------------------------------------------------------------- rules
def _ok(rule, message: str = "ok") -> RuleResult:
    return RuleResult(rule.id, rule.rule, True, None, message)


def _fail(rule, behavior: FailureBehavior, message: str, rows: pd.Index | None = None) -> RuleResult:
    return RuleResult(rule.id, rule.rule, False, behavior, message, rows if rows is not None else pd.Index([]))


def check_schema(df: pd.DataFrame, ds: Dataset, rule: SchemaRule) -> RuleResult:
    expected = {c.name for c in ds.columns}
    problems = [f"missing column {c}" for c in sorted(expected - set(df.columns))]
    problems += [f"unexpected column {c}" for c in sorted(set(df.columns) - expected)]
    for col in ds.columns:
        values = df[col.name].dropna() if col.name in df.columns else pd.Series(dtype=object)
        if not values.empty:
            if not _DTYPE_CHECKS[col.type](values):
                problems.append(f"{col.name}: expected {col.type.value}, got {df[col.name].dtype}")
    return _fail(rule, rule.on_fail, "; ".join(problems)) if problems else _ok(rule)


def check_not_null(df: pd.DataFrame, ds: Dataset, rule: NotNullRule) -> RuleResult:
    required = [c.name for c in ds.columns if not c.nullable]
    mask = df[required].isna().any(axis=1)
    if not mask.any():
        return _ok(rule)
    cols = [c for c in required if df[c].isna().any()]
    return _fail(rule, rule.on_fail, f"{int(mask.sum())} rows with nulls in {cols}", df.index[mask])


def check_unique(df: pd.DataFrame, rule: UniqueRule) -> RuleResult:
    keep = "first" if rule.on_fail is FailureBehavior.QUARANTINE_ROWS else False
    mask = df.duplicated(subset=rule.columns, keep=keep)
    if not mask.any():
        return _ok(rule)
    return _fail(rule, rule.on_fail, f"{int(mask.sum())} duplicate rows on {rule.columns}", df.index[mask])


def _luhn_ok(candidate: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", candidate)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _hits(text: str, pattern: str) -> bool:
    found = PII_PATTERNS[pattern].findall(text)
    if pattern == "card_number":
        return any(_luhn_ok(m) for m in found)
    return bool(found)


def check_pii_scan(df: pd.DataFrame, rule: PiiScanRule) -> RuleResult:
    hits: dict[str, pd.Series] = {}
    for p in rule.patterns:
        mask = pd.Series(False, index=df.index)
        for col in rule.columns:
            text = df[col].dropna().astype(str)
            mask |= text.map(lambda v, p=p: _hits(v, p)).reindex(df.index, fill_value=False)
        if mask.any():
            hits[p] = mask
    if not hits:
        return _ok(rule)
    any_hit = pd.concat(hits.values(), axis=1).any(axis=1)
    found = ", ".join(f"{p}: {int(m.sum())}" for p, m in hits.items())
    incident = sorted(set(hits) & set(rule.incident_patterns))
    behavior = FailureBehavior.SECURITY_INCIDENT if incident else rule.on_fail
    note = f" (Prohibited data: {', '.join(incident)})" if incident else ""
    return _fail(rule, behavior, f"PII found in {rule.columns}: {found}{note}", df.index[any_hit])


def check_freshness(df: pd.DataFrame, rule: FreshnessRule, ctx: CheckContext) -> RuleResult:
    if rule.max_age_minutes is not None:
        latest = pd.to_datetime(df[rule.timestamp_column], utc=True).max()
        age = ctx.now - latest.to_pydatetime() if pd.notna(latest) else None
        fresh = age is not None and age <= timedelta(minutes=rule.max_age_minutes)
        detail = f"latest {rule.timestamp_column} is {age} old (limit {rule.max_age_minutes} min)"
    else:
        local_now = ctx.now.astimezone(ctx.local_tz)
        # After the deadline the last complete day (yesterday) must be present.
        lag_days = 1 if local_now.time() >= rule.deadline_local else 2
        expected = local_now.date() - timedelta(days=lag_days)
        part = df[rule.partition_column]
        if pd.api.types.is_datetime64_any_dtype(part):
            part = pd.to_datetime(part, utc=True).dt.tz_convert(ctx.local_tz).dt.date
        latest = part.max() if not part.empty else None
        fresh = latest is not None and pd.notna(latest) and latest >= expected
        detail = f"latest {rule.partition_column} is {latest}; expected >= {expected} at {local_now:%H:%M}"

    if fresh:
        return _ok(rule, detail)
    misses = ctx.consecutive_failures.get(rule.id, 0) + 1
    behavior = rule.escalate_to if misses >= rule.escalate_after_consecutive else rule.on_fail
    return _fail(rule, behavior, f"stale: {detail}; consecutive misses {misses}")


def check_consent_scope(df: pd.DataFrame, rule: ConsentScopeRule, ctx: CheckContext) -> RuleResult:
    missing_gates = sorted(set(rule.required_gates) - ctx.gates_cleared)
    if missing_gates:
        return _fail(
            rule, rule.escalate_to, f"purpose {rule.purpose} blocked: review gate(s) {missing_gates} not cleared", df.index
        )
    if ctx.consent is None:
        return _fail(rule, rule.escalate_to, "no consent state supplied; failing closed", df.index)

    consent = ctx.consent.reindex(df[rule.subject_column].values)
    allowed = pd.Series(True, index=df.index)
    for attr, required in rule.require.items():
        values = consent[attr].to_numpy() if attr in consent else [None] * len(df)
        # Unknown consent state fails closed.
        allowed &= pd.Series([v is not None and pd.notna(v) and bool(v) == required for v in values], index=df.index)
    if allowed.all():
        return _ok(rule)
    bad = df.index[~allowed]
    share = len(bad) / len(df)
    behavior = rule.escalate_to if share > rule.max_quarantine_share else rule.on_fail
    return _fail(
        rule,
        behavior,
        f"{len(bad)} rows ({share:.1%}) out of scope for purpose {rule.purpose} "
        f"(limit {rule.max_quarantine_share:.0%})",
        bad,
    )


def run_rule(df: pd.DataFrame, ds: Dataset, rule, ctx: CheckContext) -> RuleResult:
    match rule:
        case SchemaRule():
            return check_schema(df, ds, rule)
        case NotNullRule():
            return check_not_null(df, ds, rule)
        case UniqueRule():
            return check_unique(df, rule)
        case PiiScanRule():
            return check_pii_scan(df, rule)
        case FreshnessRule():
            return check_freshness(df, rule, ctx)
        case ConsentScopeRule():
            return check_consent_scope(df, rule, ctx)
    raise TypeError(f"unknown rule type {type(rule).__name__}")


# ------------------------------------------------------------------ engine
def evaluate(df: pd.DataFrame, ds: Dataset, ctx: CheckContext) -> Outcome:
    """Run every governance rule in the contract and decide what to publish."""
    results: list[RuleResult] = []
    for rule in ds.governance:
        result = run_rule(df, ds, rule, ctx)
        results.append(result)
        # Row-level rules cannot run on a batch whose columns do not match.
        if isinstance(rule, SchemaRule) and not result.passed:
            break

    failed = [r for r in results if not r.passed]
    publish = not any(r.behavior.blocking for r in failed)
    incidents = [f"{ds.name} {r.rule_id}: {r.message}" for r in failed if r.behavior is FailureBehavior.SECURITY_INCIDENT]
    alerts = [
        f"[{asset_check_severity(r.behavior)}] {ds.name} {r.rule_id} -> {r.behavior.value}: {r.message}" for r in failed
    ]
    stale_banner = any(r.rule == "freshness" and r.behavior is FailureBehavior.SERVE_STALE_AND_ALERT for r in failed)

    # Quarantine rows from quarantine and incident rules; incident rows are also purged downstream.
    reasons: dict[object, list[str]] = {}
    for r in failed:
        if r.behavior in (FailureBehavior.QUARANTINE_ROWS, FailureBehavior.SECURITY_INCIDENT):
            for idx in r.failing_rows:
                reasons.setdefault(idx, []).append(r.rule_id)
    q_index = pd.Index(list(reasons))
    quarantined = df.loc[q_index].assign(_quarantine_reason=[",".join(reasons[i]) for i in q_index])
    published = df.drop(index=q_index) if publish else df.iloc[0:0]

    return Outcome(ds.name, publish, published, quarantined, results, alerts, incidents, stale_banner)
