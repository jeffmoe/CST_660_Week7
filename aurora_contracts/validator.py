"""Contract validator: run a dataset's contract and governance rules on a batch
and return a pass/fail verdict with an exit code.

This is the gate the orchestrator runs before publishing an asset (ADR-002).
It adds no rules of its own: every check, threshold and failure behavior comes
from contracts/schemas/*.yaml through aurora_contracts.governance.

Exit codes
  0  PASS     every rule passed
  1  FAIL     violations found; offending rows quarantined, the rest publishable
  2  BLOCKED  a blocking rule failed (block_publish or security_incident); nothing publishes
With fail_on="blocking", exit code 1 is downgraded to 0 (quarantine is accepted).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .governance import CheckContext, Outcome, asset_check_severity, evaluate
from .models import Dataset

PASS, FAIL, BLOCKED = 0, 1, 2
VERDICT = {PASS: "PASS", FAIL: "FAIL", BLOCKED: "FAIL (BLOCKED)"}


@dataclass
class ValidationResult:
    dataset: Dataset
    outcome: Outcome
    exit_code: int
    rows_in: int

    @property
    def verdict(self) -> str:
        return VERDICT[self.exit_code]

    def failed_rules(self) -> dict[str, str]:
        return {r.rule_id: r.behavior.value for r in self.outcome.failed}

    def to_dict(self) -> dict:
        o = self.outcome
        return {
            "dataset": self.dataset.name,
            "contract_version": self.dataset.version,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "rows_in": self.rows_in,
            "rows_publishable": len(o.published),
            "rows_quarantined": len(o.quarantined),
            "publish": o.publish,
            "rules": [
                {"rule": r.rule_id, "type": r.rule, "passed": r.passed,
                 "behavior": r.behavior.value if r.behavior else None,
                 "severity": asset_check_severity(r.behavior) if r.behavior else None,
                 "failing_rows": len(r.failing_rows), "message": r.message}
                for r in o.results
            ],
            "skipped_rules": [r.id for r in self.dataset.governance if r.id not in {x.rule_id for x in o.results}],
            "incidents": o.incidents,
            "stale_banner": o.stale_banner,
        }


def validate(df: pd.DataFrame, ds: Dataset, ctx: CheckContext, fail_on: str = "any") -> ValidationResult:
    if fail_on not in ("any", "blocking"):
        raise ValueError("fail_on must be 'any' or 'blocking'")
    outcome = evaluate(df, ds, ctx)
    if not outcome.publish:
        code = BLOCKED
    elif outcome.failed and fail_on == "any":
        code = FAIL
    else:
        code = PASS
    return ValidationResult(ds, outcome, code, len(df))


def format_report(result: ValidationResult, key_columns: list[str] | None = None, max_rows: int = 5) -> str:
    o, ds = result.outcome, result.dataset
    lines = [
        f"Contract validator: {ds.name} v{ds.version} ({result.rows_in:,} rows)",
        "-" * 72,
    ]
    ran = {r.rule_id: r for r in o.results}
    for rule in ds.governance:
        r = ran.get(rule.id)
        if r is None:
            lines.append(f"  SKIP  {rule.id:<10} {rule.rule:<14} not run (schema check failed)")
        elif r.passed:
            lines.append(f"  PASS  {rule.id:<10} {rule.rule:<14}")
        else:
            sev = asset_check_severity(r.behavior)
            lines.append(f"  FAIL  {rule.id:<10} {rule.rule:<14} [{sev}] {r.behavior.value}: {r.message}")
    if len(o.quarantined):
        cols = [c for c in (key_columns or ds.primary_key) if c in o.quarantined.columns] + ["_quarantine_reason"]
        lines.append("")
        lines.append(f"  Quarantined rows ({len(o.quarantined)}):")
        for row in o.quarantined[cols].head(max_rows).itertuples(index=False):
            lines.append("    " + ", ".join(f"{c}={v}" for c, v in zip(cols, row)))
        if len(o.quarantined) > max_rows:
            lines.append(f"    ... {len(o.quarantined) - max_rows} more")
    for inc in o.incidents:
        lines.append(f"  INCIDENT  {inc}")
    lines += [
        "-" * 72,
        f"VERDICT: {result.verdict}  (exit {result.exit_code})  "
        f"publish={o.publish}  publishable={len(o.published):,}  quarantined={len(o.quarantined):,}",
    ]
    return "\n".join(lines)
