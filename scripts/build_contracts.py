"""Validate the contract layer and generate the schema it feeds.

Loads contracts/consumers.yaml and contracts/schemas/*.yaml (typed by pydantic),
checks conformance to ADR-001..003, the security matrix and the assumptions
register, then writes:

  contracts/generated/dbt/<dialect>/schema.yml           dbt model contracts + tests
  contracts/generated/jsonschema/<dataset>.schema.json   producer event schema
  docs/contract_layer.md                                 register, schemas, rules, conformance

Exits non-zero if any conformance check fails.

Usage:
    python scripts/build_contracts.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora_contracts import codegen, conformance, loader  # noqa: E402
from aurora_contracts.governance import asset_check_severity  # noqa: E402
from aurora_contracts.models import FailureBehavior  # noqa: E402

GENERATED = ROOT / "contracts" / "generated"
DOC = ROOT / "docs" / "contract_layer.md"

BEHAVIOR_TEXT = {
    FailureBehavior.BLOCK_PUBLISH: "Blocking asset check fails; the batch is not published and consumers keep the last good version; owner alerted.",
    FailureBehavior.QUARANTINE_ROWS: "Offending rows move to a restricted quarantine table with the rule ID; the rest publish. Escalates when the quarantined share passes the rule's limit.",
    FailureBehavior.SERVE_STALE_AND_ALERT: "Consumers keep the last good version with a stale banner showing its as-of time; owner alerted; escalates after N consecutive misses.",
    FailureBehavior.SECURITY_INCIDENT: "Batch blocked, offending rows quarantined for purge (R-04), security incident opened and Security lead paged.",
    FailureBehavior.WARN: "Batch publishes; non-blocking alert to the owner.",
}


def md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False)


def render(layer: loader.ContractLayer, findings: list[conformance.Finding], outputs: list[Path]) -> str:
    reg = layer.register
    register = pd.DataFrame(
        [
            {
                "ID": c.id,
                "Consumer": c.name,
                "Owner": c.owner,
                "Datasets": ", ".join(c.datasets),
                "Roles": ", ".join(c.roles),
                "Purposes": ", ".join(c.purposes),
                "Max class": c.max_classification,
                "Gates": ", ".join(c.required_gates) or "—",
                "SLA": c.sla.availability,
            }
            for c in reg.consumers
        ]
    )

    contracts = []
    for c in reg.consumers:
        guarantees = "\n".join(f"- {g}" for g in c.guarantees)
        contracts.append(
            f"### {c.id}: {c.name}\n\n{c.description}\n\n"
            f"**Reads:** {', '.join(f'`{d}`' for d in c.datasets)} · **Roles:** {', '.join(c.roles)} · "
            f"**Purposes:** {', '.join(c.purposes)} · **Max classification:** {c.max_classification}\n\n"
            f"**SLA:** {c.sla.availability}\n\n**On breach:** {' '.join(c.on_breach.split())}\n\n"
            f"**Guarantees:**\n\n{guarantees}\n\n**Change policy:** {' '.join(c.change_policy.split())} "
            f"Breaking changes approved by: {', '.join(c.breaking_change_approvers)}."
        )

    schemas = []
    for ds in layer.datasets.values():
        cols = pd.DataFrame(
            [
                {
                    "Column": c.name,
                    "Type": c.type.value,
                    "Null?": "yes" if c.nullable else "no",
                    "Class": c.classification,
                    "PII": c.pii_element or ("—" if not c.pii else "yes"),
                    "Stable": "✓" if c.stable else "",
                    "Constraints": "; ".join(
                        x
                        for x in (
                            "unique" if c.unique else "",
                            f"in {c.accepted_values}" if c.accepted_values else "",
                            f">= {c.min_value:g}" if c.min_value is not None else "",
                            f"<= {c.max_value:g}" if c.max_value is not None else "",
                        )
                        if x
                    ),
                    "Description": c.description,
                }
                for c in ds.columns
            ]
        )
        consumers = ", ".join(c.id for c in layer.consumers_of(ds.name)) or "internal (dbt models)"
        lookback = f" · lookback {ds.incremental_lookback_days} d" if ds.incremental_lookback_days else ""
        schemas.append(
            f"### `{ds.name}` v{ds.version}\n\n{' '.join(ds.description.split())}\n\n"
            f"**Grain:** {ds.grain} · **Key:** {', '.join(ds.primary_key)} · **Produced by:** `{ds.produced_by}` · "
            f"**Schedule:** {ds.schedule} · **Materialization:** {ds.materialization}{lookback} · "
            f"**Classification:** {ds.classification} · **Retention:** {ds.retention_rule} · "
            f"**Consumers:** {consumers}\n\n{md(cols)}"
        )

    rules = pd.DataFrame(
        [
            {
                "Rule": r.id,
                "Dataset": ds.name,
                "Type": r.rule,
                "On fail": r.on_fail.value,
                "Asset check": asset_check_severity(r.on_fail),
                "Escalation": (
                    f"→ {r.escalate_to.value} after {r.escalate_after_consecutive} misses"
                    if r.rule == "freshness"
                    else f"→ {r.escalate_to.value} if > {r.max_quarantine_share:.0%} rows"
                    if r.rule == "consent_scope"
                    else f"→ security_incident on {', '.join(r.incident_patterns)}"
                    if r.rule == "pii_scan" and r.incident_patterns
                    else "—"
                ),
                "Behavior": " ".join(r.description.split()),
            }
            for ds in layer.datasets.values()
            for r in ds.governance
        ]
    )
    behaviors = pd.DataFrame(
        [{"Failure behavior": b.value, "Blocking": "yes" if b.blocking else "no", "What happens": t} for b, t in BEHAVIOR_TEXT.items()]
    )

    counts = Counter(f.basis.split(" ")[0] for f in findings)
    summary = pd.DataFrame(
        [
            {"Basis": basis, "Checks": n, "Failed": sum(1 for f in findings if f.basis.split(" ")[0] == basis and not f.passed)}
            for basis, n in sorted(counts.items())
        ]
    )
    detail = pd.DataFrame(
        [{"Check": f.check, "Basis": f.basis, "Subject": f.subject, "Result": "pass" if f.passed else "**FAIL**", "Detail": f.detail} for f in findings]
    )

    outputs_md = "\n".join(f"- `{p.relative_to(ROOT).as_posix()}`" for p in outputs)

    return f"""# Contract Layer — Subscription Health Data Product

_Generated by `scripts/build_contracts.py` from `contracts/consumers.yaml` and
`contracts/schemas/*.yaml` (register v{reg.version}, last reviewed {reg.last_reviewed}).
Do not edit by hand._

The contract layer sits between the warehouse models (ADR-001, dbt), the
orchestrator's asset checks (ADR-002) and the micro-batch event feed (ADR-003) on one
side, and the three consumers on the other. Every contract is validated against those
ADRs, the security matrix and the assumptions register, and it generates the physical
schema the spec uses:

{outputs_md}

## 1. Consumer register

{md(register)}

## 2. Consumer contracts

{(chr(10) * 2).join(contracts)}

## 3. Schema definitions

{(chr(10) * 2).join(schemas)}

## 4. Governance rules as code

Implemented in `aurora_contracts/governance.py`, configured per dataset in
`contracts/schemas/*.yaml`, and tested in `tests/test_governance.py`. Each rule runs
as an orchestrator asset check (ADR-002): `ERROR` checks block publish, `WARN` checks
alert. Unknown consent state fails closed.

### Failure behaviors

{md(behaviors)}

### Rule catalog

{md(rules)}

## 5. Conformance to ADR-001..003, security matrix and assumptions

{md(summary)}

<details><summary>All {len(findings)} checks</summary>

{md(detail)}

</details>
"""


def main() -> int:
    layer = loader.load()
    findings = conformance.check(layer)
    failed = [f for f in findings if not f.passed]

    outputs: list[Path] = []
    for dialect in codegen.DIALECTS:
        path = GENERATED / "dbt" / dialect / "schema.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Generated by scripts/build_contracts.py for the {dialect} candidate (ADR-001). Do not edit.\n"
            + yaml.safe_dump(codegen.dbt_schema(layer, dialect), sort_keys=False, width=100),
            encoding="utf-8",
        )
        outputs.append(path)
    for ds in layer.datasets.values():
        if ds.layer == "raw":
            path = GENERATED / "jsonschema" / f"{ds.name}.schema.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(codegen.event_json_schema(ds), indent=2) + "\n", encoding="utf-8")
            outputs.append(path)

    DOC.write_text(render(layer, findings, outputs), encoding="utf-8")

    print(f"{len(layer.register.consumers)} consumers, {len(layer.datasets)} contracts, "
          f"{sum(len(d.governance) for d in layer.datasets.values())} governance rules")
    print(f"Conformance: {len(findings) - len(failed)}/{len(findings)} checks passed")
    for f in failed:
        print(f"  FAIL {f.check} [{f.basis}] {f.subject}: {f.detail}", file=sys.stderr)
    for p in [*outputs, DOC]:
        print(f"Wrote {p.relative_to(ROOT).as_posix()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
