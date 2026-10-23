"""Build the security & privacy matrix from security/data_matrix.yaml.

The matrix is coupled to the assumptions register:
  * which regimes are in scope is resolved per scenario from the A-REG-*
    assumptions (subscriber jurisdictions, health-data status, card handling)
  * retention windows are then resolved from the in-scope regimes' legal
    minimums and maximums, and raw-event retention comes from A-STO-02
  * the CCPA revenue threshold is tested against projected revenue
    (subscriber curve x A-REV-01)

Validation fails (non-zero exit) on dangling references, a retention conflict
(legal minimum above a cap), raw-event retention shorter than the storage
projections assume, Restricted/Prohibited data without the required controls,
or missing cross-border / re-identification review gates.

Writes docs/security_matrix.md (standalone).

Usage:
    python scripts/build_security_matrix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml

from build_assumptions import ROOT, SCENARIOS, load_assumptions, project
from build_evidence import CLAIMS_FILE, SOURCES_FILE

MATRIX_FILE = ROOT / "security" / "data_matrix.yaml"
MATRIX_MD = ROOT / "docs" / "security_matrix.md"

CCPA_REVENUE_THRESHOLD = 26_625_000  # USD, CPPA CPI adjustment effective 2025-01-01
FLAG = "⚑"


def load_matrix(path: Path = MATRIX_FILE) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------- resolution
def regimes_in_scope(m: dict, a: dict, scenario: str) -> set[str]:
    in_scope = set()
    for r in m["regimes"]:
        if all(a[aid][scenario] in allowed for aid, allowed in r["in_scope_when"].items()):
            in_scope.add(r["id"])
    return in_scope


def resolve_retention(m: dict, a: dict, scenario: str, scope: set[str]) -> dict[str, dict]:
    rules = {r["id"]: r for r in m["retention_rules"]}
    resolved: dict[str, dict] = {}

    def resolve(rid: str) -> dict:
        if rid in resolved:
            return resolved[rid]
        r = rules[rid]
        policy = a[r["policy_from"]][scenario] if "policy_from" in r else r["policy_months"]
        mins = {g: v for g, v in r.get("min", {}).items() if g in scope}
        caps = {g: v for g, v in r.get("max", {}).items() if g in scope}
        if "max_from" in r:
            caps[r["max_from"]] = resolve(r["max_from"])["months"]

        floor = max([policy, *mins.values()])
        cap = min(caps.values()) if caps else None
        months = floor if cap is None else min(floor, cap)

        if months == policy:
            driver = f"policy ({r['policy_from']})" if "policy_from" in r else "policy"
        elif mins and months == max(mins.values()) and months > policy:
            driver = "legal minimum: " + ", ".join(g for g, v in mins.items() if v == months)
        else:
            driver = "cap: " + ", ".join(g for g, v in caps.items() if v == months)

        resolved[rid] = {
            "months": months,
            "policy": policy,
            "driver": driver,
            "conflict": bool(mins) and cap is not None and max(mins.values()) > cap,
        }
        return resolved[rid]

    for rid in rules:
        resolve(rid)
    return resolved


def ccpa_revenue_test(meta: dict, a: dict) -> pd.DataFrame:
    proj = project(meta, a)
    rows = []
    for s in SCENARIOS:
        p = proj[(proj["scenario"] == s) & (proj["month"] > 0)]
        arpu = a["A-REV-01"][s]
        run_rate = a["A-GRW-01"][s] * arpu * 12
        fy_revenue = float((p["subscribers"] * arpu).sum())
        rows.append(
            {
                "Scenario": s,
                "Current run-rate (USD/yr)": f"{run_rate:,.0f}",
                "Projected FY revenue (USD)": f"{fy_revenue:,.0f}",
                "Run-rate over threshold?": "yes" if run_rate > CCPA_REVENUE_THRESHOLD else "no",
                "FY revenue over threshold?": "yes" if fy_revenue > CCPA_REVENUE_THRESHOLD else "no",
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------- validation
def validate(m: dict, a: dict, resolved: dict[str, dict[str, dict]]) -> list[str]:
    errors: list[str] = []
    levels = {c["level"] for c in m["classification"]}
    regimes = {r["id"] for r in m["regimes"]}
    purposes = {p["id"] for p in m["consent_purposes"]}
    rules = {r["id"] for r in m["retention_rules"]}
    abac = {p["id"] for p in m["abac_policies"]}
    gates = {g["id"] for g in m["review_gates"]}
    obligations = {o["id"] for o in m["obligations"]}
    claims = set(pd.read_csv(CLAIMS_FILE, dtype=str)["claim_id"])
    sources = set(pd.read_csv(SOURCES_FILE, dtype=str)["source_id"])
    known = regimes | purposes | rules | abac | gates | obligations | claims | sources | set(a)

    for r in m["regimes"]:
        for aid in r["in_scope_when"]:
            if aid not in a:
                errors.append(f"{r['id']}: scope depends on unknown assumption {aid}")
    for r in m["retention_rules"]:
        for key in ("min", "max"):
            for g in r.get(key, {}):
                if g not in regimes:
                    errors.append(f"{r['id']}: {key} references unknown regime {g}")
        if r.get("policy_from") and r["policy_from"] not in a:
            errors.append(f"{r['id']}: policy_from unknown assumption {r['policy_from']}")
        if r.get("max_from") and r["max_from"] not in rules:
            errors.append(f"{r['id']}: max_from unknown rule {r['max_from']}")

    used_purposes: set[str] = set()
    for e in m["pii_inventory"]:
        eid = e["id"]
        if e["classification"] not in levels:
            errors.append(f"{eid}: unknown classification {e['classification']}")
        if e["retention"] not in rules:
            errors.append(f"{eid}: unknown retention rule {e['retention']}")
        for ref, pool, kind in (
            (e["purposes"], purposes, "consent purpose"),
            (e["access_policies"], abac, "ABAC policy"),
            (e["regimes"], regimes, "regime"),
        ):
            for x in ref:
                if x not in pool:
                    errors.append(f"{eid}: unknown {kind} {x}")
        used_purposes.update(e["purposes"])
        if e["classification"] == "C3" and not {"ABAC-01", "ABAC-04", "ABAC-05"} & set(e["access_policies"]):
            errors.append(f"{eid}: Restricted (C3) data needs masking or location/service controls")
        if e["classification"] == "C4":
            if "ABAC-07" not in e["access_policies"]:
                errors.append(f"{eid}: Prohibited (C4) data needs DLP (ABAC-07)")
            if any(res[e["retention"]]["months"] != 0 for res in resolved.values()):
                errors.append(f"{eid}: Prohibited (C4) data must resolve to 0-month retention")

    for p in purposes - used_purposes:
        errors.append(f"consent purpose {p} is not used by any inventory element")
    for g in regimes - {o["regime"] for o in m["obligations"]}:
        errors.append(f"regime {g} has no mapped obligation")
    for o in m["obligations"]:
        for c in o["controls"]:
            if c not in known:
                errors.append(f"{o['id']}: unknown control {c}")
    for g in m["review_gates"]:
        for ref in g["related"]:
            if ref not in known:
                errors.append(f"{g['id']}: unknown related reference {ref}")
        if not g["reviewers"]:
            errors.append(f"{g['id']}: no named reviewers")
    risks = {g["risk"] for g in m["review_gates"]}
    for needed in ("cross-border", "re-identification"):
        if needed not in risks and "both" not in risks:
            errors.append(f"no review gate covers {needed} risk")

    for s, res in resolved.items():
        for rid, r in res.items():
            if r["conflict"]:
                errors.append(f"{rid} ({s}): legal minimum exceeds retention cap")
        # Coupling: storage projections assume A-STO-02 months of raw events.
        raw = next(r["id"] for r in m["retention_rules"] if r.get("policy_from") == "A-STO-02")
        if res[raw]["months"] < a["A-STO-02"][s]:
            errors.append(
                f"{raw} ({s}): permitted {res[raw]['months']} months < A-STO-02 "
                f"{a['A-STO-02'][s]} months assumed by storage projections"
            )
    return errors


# ---------------------------------------------------------------- rendering
def fmt_months(n: float) -> str:
    if n == 0:
        return "0 (never stored)"
    if n < 2:
        return f"{n:g} month (~{round(n * 30)} days)"
    return f"{n:g} months" + (f" ({n / 12:g} yr)" if n % 12 == 0 else "")


def md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False)


def render(meta: dict, m: dict, a: dict, scope: dict[str, set[str]], resolved: dict, ccpa: pd.DataFrame) -> str:
    stated = m["meta"]["stated_scope_scenario"]
    regimes = {r["id"]: r for r in m["regimes"]}
    rules = {r["id"]: r for r in m["retention_rules"]}

    jurisdiction = pd.DataFrame(
        [
            {"ID": aid, "Assumption": x["name"], **{s: x[s] for s in SCENARIOS}, "Owner": x["owner"]}
            for aid, x in a.items()
            if x.get("jurisdiction_assumption")
        ]
    )

    scope_tbl = pd.DataFrame(
        [
            {
                "Regime": f"{r['name']}" + (f" {FLAG}" if r["jurisdiction_assumption"] else ""),
                "ID": r["id"],
                "In scope when": "; ".join(f"{k} ∈ {{{', '.join(v)}}}" for k, v in r["in_scope_when"].items()),
                **{s: "✓" if r["id"] in scope[s] else "—" for s in SCENARIOS},
            }
            for r in m["regimes"]
        ]
    )

    classes = pd.DataFrame(
        [
            {
                "Level": c["level"],
                "Name": c["name"],
                "Definition": c["definition"],
                "Examples": c["examples"],
                "Handling": c["handling"],
                "External model provider": c["llm_provider"],
                "AI agent access": c["agent_access"],
                "Export": c["export"],
            }
            for c in m["classification"]
        ]
    )

    inventory_sections = []
    for domain in dict.fromkeys(e["domain"] for e in m["pii_inventory"]):
        rows = []
        for e in (x for x in m["pii_inventory"] if x["domain"] == domain):
            regime_cells = [
                g if g in scope[stated] else f"({g})" for g in e["regimes"]
            ]
            rows.append(
                {
                    "ID": e["id"],
                    "Element": e["element"],
                    "Fields": e["fields"],
                    "Identifiability": e["identifiability"],
                    "Class": e["classification"],
                    "System of record": e["system_of_record"],
                    "Warehouse layer": e["warehouse_layer"],
                    "Purposes": ", ".join(e["purposes"]),
                    "Retention": f"{e['retention']}: {fmt_months(resolved[stated][e['retention']]['months'])}",
                    "Access": ", ".join(e["access_policies"]),
                    "Regimes": ", ".join(regime_cells),
                }
            )
        inventory_sections.append(f"### {domain}\n\n{md(pd.DataFrame(rows))}")

    consent = pd.DataFrame(
        [
            {
                "ID": p["id"],
                "Purpose": p["purpose"],
                "US basis": p["us_basis"],
                "GDPR basis (if in scope)": p["gdpr_basis"],
                "Mechanism": p["mechanism"],
                "Withdrawal": p["withdrawal"],
                "Propagation SLA": p["propagation_sla"],
                "Enforced by attribute": p["enforcement_attribute"],
            }
            for p in m["consent_purposes"]
        ]
    )

    retention = pd.DataFrame(
        [
            {
                "Rule": rid,
                "Data type": r["data_type"],
                "Clock starts": r["clock"],
                **{s: fmt_months(resolved[s][rid]["months"]) for s in SCENARIOS},
                f"Driver ({stated})": resolved[stated][rid]["driver"],
                "End action": r["end_action"],
            }
            for rid, r in rules.items()
        ]
    )
    changed = [
        f"- **{rid}** ({rules[rid]['data_type']}): "
        + ", ".join(f"{s} = {fmt_months(resolved[s][rid]['months'])}" for s in SCENARIOS)
        + f". {' '.join(rules[rid]['basis'].split())}"
        for rid in rules
        if len({resolved[s][rid]["months"] for s in SCENARIOS}) > 1
    ]

    rbac = pd.DataFrame(
        [
            {"Role": r["role"], "Description": r["description"], "C1": r["C1"], "C2": r["C2"], "C3": r["C3"], "C4": r["C4"]}
            for r in m["rbac_roles"]
        ]
    )
    abac = pd.DataFrame(
        [
            {"ID": p["id"], "Policy": p["name"], "Rule": p["rule"], "Attributes": ", ".join(p["attributes"]), "Enforced in": p["enforced_in"]}
            for p in m["abac_policies"]
        ]
    )

    reg = pd.DataFrame(
        [
            {
                "ID": o["id"],
                "Regime": o["regime"] + (f" {FLAG}" if regimes[o["regime"]]["jurisdiction_assumption"] else ""),
                "Obligation": o["obligation"],
                "Legal reference": o["legal_ref"],
                **{s: "✓" if o["regime"] in scope[s] else "—" for s in SCENARIOS},
                "Controls in this matrix": ", ".join(o["controls"]),
                "Owner": o["owner"],
            }
            for o in m["obligations"]
        ]
    )

    gates = pd.DataFrame(
        [
            {
                "Gate": g["id"],
                "Name": g["name"],
                "Risk": g["risk"],
                "Trigger": g["trigger"],
                "Reviewers (named roles)": " + ".join(g["reviewers"]),
                "Evidence required": g["evidence_required"],
                "Blocks": g["blocks"],
                "Related": ", ".join(g["related"]),
            }
            for g in m["review_gates"]
        ]
    )

    signoff_roles = sorted({r for g in m["review_gates"] for r in g["reviewers"]} | {m["meta"]["owner"].split(" (")[0]})
    signoff = pd.DataFrame([{"Role": r, "Name": "", "Decision": "Pending", "Date": ""} for r in signoff_roles])

    jur_regimes = ", ".join(r["id"] for r in m["regimes"] if r["jurisdiction_assumption"])

    return f"""# Security & Privacy Matrix — Subscription Health Data Product

_Generated by `scripts/build_security_matrix.py` from `security/data_matrix.yaml` and
`assumptions/assumptions.yaml` (matrix v{m['meta']['matrix_version']}, last reviewed
{m['meta']['last_reviewed']}). Do not edit by hand._

**Stated regulatory scope: the `{stated}` scenario of the assumptions register.**
Which regimes apply, and therefore which retention windows hold, is resolved from the
A-REG-* assumptions per scenario. Change an assumption, re-run the script, and this
document changes with it. Legal references describe obligations; interpreting them
for Aurora is a Legal decision made at the gates below.

{FLAG} marks a **jurisdiction assumption** that Legal must confirm. Regimes resting on
one: {jur_regimes}.

## 1. Jurisdiction assumptions {FLAG}

{md(jurisdiction)}

### Regimes in scope by scenario

{md(scope_tbl)}

### CCPA revenue threshold test (${CCPA_REVENUE_THRESHOLD:,.0f}, preceding calendar year)

{md(ccpa)}

{FLAG} At today's subscriber count the revenue test may not be met yet, but projected
fiscal-year revenue crosses it in every scenario, so CCPA applies from the following
calendar year at the latest. The CCPA also applies at 100,000+ consumers or
households whose data is bought, sold or shared, which ad-platform sharing
(A-REG-06) can reach through site visitors alone. **This matrix treats CCPA as in
scope from day one**; Legal confirms at sign-off.

## 2. Data classification

{md(classes)}

## 3. PII inventory

Regimes in parentheses are not in scope under the stated (`{stated}`) scenario but
become applicable in another scenario. Retention shown is the resolved `{stated}` value.

{(chr(10) * 2).join(inventory_sections)}

## 4. Consent model

Consent state is held in an append-only consent ledger (PII-SUB-08) and exposed to
the warehouse as subscriber-level attributes that the ABAC policies in section 6
enforce. Withdrawal propagates by the SLA below; the nightly feature build (ADR-002)
reads current consent state before building features.

{md(consent)}

## 5. Retention schedule by data type

Effective window per scenario = the larger of the business policy and every in-scope
legal minimum, capped by Aurora's cap for every in-scope regime and by the window of any
source the data is derived from. A minimum above a cap fails the build.

{md(retention)}

### Windows that change with regulatory scope

{chr(10).join(changed) if changed else "None: every window is the same in all scenarios."}

**Coupling check:** raw clickstream retention (R-06) is never shorter than the A-STO-02
hot-retention window the storage projections assume, in any scenario.

## 6. Access control

### RBAC: baseline grant by classification

`full` = unmasked; `masked` = C3 columns masked; `pseudo` = surrogate IDs only;
`agg` = aggregates with k ≥ 10; `synthetic` = synthetic data only.

{md(rbac)}

### ABAC: attribute policies that narrow RBAC

{md(abac)}

## 7. Regulatory mapping

✓ = regime in scope under that scenario. {FLAG} = scope rests on a jurisdiction assumption.

{md(reg)}

## 8. Human-review gates (cross-border and re-identification)

No data flow, export, linkage, index exposure or reclassification covered by a gate
below may happen until the named reviewers have approved it and the approval is
recorded with the change. SEC-G3 is the same gate as ADR-004 G0.

{md(gates)}

## 9. Sign-off

This matrix becomes the policy of record, and the spec's data-handling section may be
written, only after every row below is signed.

{md(signoff)}
"""


def main() -> int:
    meta, a = load_assumptions()
    m = load_matrix()

    scope = {s: regimes_in_scope(m, a, s) for s in SCENARIOS}
    resolved = {s: resolve_retention(m, a, s, scope[s]) for s in SCENARIOS}

    errors = validate(m, a, resolved)
    if errors:
        print("Security matrix validation failed:", *errors, sep="\n  - ", file=sys.stderr)
        return 1

    MATRIX_MD.parent.mkdir(parents=True, exist_ok=True)
    MATRIX_MD.write_text(render(meta, m, a, scope, resolved, ccpa_revenue_test(meta, a)), encoding="utf-8")
    stated = m["meta"]["stated_scope_scenario"]
    print(
        f"Wrote {MATRIX_MD.relative_to(ROOT)}: {len(m['pii_inventory'])} PII elements, "
        f"{len(m['retention_rules'])} retention rules, regimes in scope ({stated}): "
        f"{', '.join(sorted(scope[stated]))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
