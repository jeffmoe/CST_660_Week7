"""Validate the evidence table and own-testing claims, then render Markdown.

Reads:
  evidence/evidence_sources.csv     one row per source, labeled by evidence type
  evidence/own_testing_claims.csv   claims that only Aurora's own testing can settle
  assumptions/assumptions.yaml      to cross-check own-testing assumptions

Writes:
  docs/evidence_table.md

Validation fails (non-zero exit) if a source has an unknown evidence type, a
decision area lacks an own-testing row, a claim id is dangling, or an
assumption flagged `own_testing: true` has no claim covering it.

Usage:
    python scripts/build_evidence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from build_assumptions import ROOT, load_assumptions

SOURCES_FILE = ROOT / "evidence" / "evidence_sources.csv"
CLAIMS_FILE = ROOT / "evidence" / "own_testing_claims.csv"
EVIDENCE_MD = ROOT / "docs" / "evidence_table.md"

EVIDENCE_TYPES = (
    "vendor marketing",
    "peer-reviewed",
    "practitioner benchmark",
    "own testing",
)
DECISION_AREAS = (
    "Warehouse vs lakehouse",
    "Orchestration",
    "Streaming vs micro-batch",
    "Agentic AI",
    "Cross-cutting baselines",
    "Security and privacy",
)
OWN_TESTING_FLAG = "⚑ OWN TESTING"


def split_ids(cell: object) -> list[str]:
    if pd.isna(cell) or not str(cell).strip():
        return []
    return [part.strip() for part in str(cell).split(";") if part.strip()]


def validate(sources: pd.DataFrame, claims: pd.DataFrame, assumptions: dict) -> list[str]:
    errors: list[str] = []

    for frame, key in ((sources, "source_id"), (claims, "claim_id")):
        dupes = frame[frame[key].duplicated()][key].tolist()
        if dupes:
            errors.append(f"Duplicate {key}: {dupes}")

    bad_types = sorted(set(sources["evidence_type"]) - set(EVIDENCE_TYPES))
    if bad_types:
        errors.append(f"Unknown evidence_type values: {bad_types}")

    for df, name in ((sources, "sources"), (claims, "claims")):
        bad_areas = sorted(set(df["decision_area"]) - set(DECISION_AREAS))
        if bad_areas:
            errors.append(f"Unknown decision_area in {name}: {bad_areas}")

    for area in DECISION_AREAS:
        types = set(sources.loc[sources["decision_area"] == area, "evidence_type"])
        if "own testing" not in types:
            errors.append(f"{area}: no own-testing evidence row")

    claim_ids = set(claims["claim_id"])
    referenced = {c for cell in sources["informs_claims"] for c in split_ids(cell)}
    if dangling := sorted(referenced - claim_ids):
        errors.append(f"Sources reference unknown claims: {dangling}")

    own_rows = sources[sources["evidence_type"] == "own testing"]
    covered = {c for cell in own_rows["informs_claims"] for c in split_ids(cell)}
    if untested := sorted(claim_ids - covered):
        errors.append(f"Claims with no own-testing evidence row: {untested}")

    linked = {a for cell in claims["related_assumptions"] for a in split_ids(cell)}
    if unknown := sorted(linked - set(assumptions)):
        errors.append(f"Claims reference unknown assumptions: {unknown}")
    flagged = {aid for aid, a in assumptions.items() if a["own_testing"]}
    if orphaned := sorted(flagged - linked):
        errors.append(f"own_testing assumptions with no claim: {orphaned}")

    return errors


def render(sources: pd.DataFrame, claims: pd.DataFrame) -> str:
    counts = pd.crosstab(sources["decision_area"], sources["evidence_type"])
    counts = counts.reindex(index=list(DECISION_AREAS), columns=list(EVIDENCE_TYPES), fill_value=0)

    sections = []
    for area in DECISION_AREAS:
        rows = sources[sources["decision_area"] == area].copy()
        if rows.empty:
            continue
        rows["Flag"] = rows["evidence_type"].map(
            lambda t: OWN_TESTING_FLAG if t == "own testing" else ""
        )
        rows["Claims"] = rows["informs_claims"].map(lambda c: ", ".join(split_ids(c)))
        table = rows.rename(
            columns={
                "source_id": "ID",
                "evidence_type": "Type",
                "source": "Source",
                "year": "Year",
                "locator": "Locator",
                "relevance_note": "Relevance",
            }
        )[["ID", "Type", "Flag", "Source", "Year", "Locator", "Relevance", "Claims"]]
        area_claims = claims[claims["decision_area"] == area]["claim_id"].tolist()
        sections.append(
            f"### {area}\n\n{table.to_markdown(index=False)}\n\n"
            f"**Cannot be settled externally:** {', '.join(area_claims)}"
        )

    claim_blocks = []
    for _, c in claims.iterrows():
        assumptions = ", ".join(split_ids(c["related_assumptions"])) or "—"
        claim_blocks.append(
            f"#### {OWN_TESTING_FLAG} — {c['claim_id']} ({c['decision_area']})\n\n"
            f"**Claim:** {c['claim']}\n\n"
            f"- **Why external sources cannot settle it:** {c['why_not_externally_sourceable']}\n"
            f"- **Related assumptions:** {assumptions}\n"
            f"- **Test method:** {c['test_method']}\n"
            f"- **Pass threshold:** {c['pass_threshold']}\n"
            f"- **Status:** {c['status']}"
        )

    return f"""# Evidence Table — Subscription Health Data Product

_Generated by `scripts/build_evidence.py` from `evidence/evidence_sources.csv` and
`evidence/own_testing_claims.csv`. Do not edit by hand._

Every source is labeled with exactly one evidence type:

| Type | Meaning | How to weigh it |
|:--|:--|:--|
| vendor marketing | Published by a vendor about its own product (blogs, docs, reports) | Indicates capabilities and intent; treat performance and cost claims as unverified |
| peer-reviewed | Refereed conference or journal paper | Strong on mechanisms and trade-offs; workloads rarely match Aurora's scale |
| practitioner benchmark | Reproducible benchmark or survey by practitioners, often vendor-adjacent | Useful directional comparison; check who ran it and on what data |
| own testing | Aurora's own PoC, pilot or measurement | Only source that can confirm Aurora-specific claims; planned, not yet run |

Rows marked **{OWN_TESTING_FLAG}** are claims that **cannot be sourced externally**
and must be proven by Aurora's own testing before the corresponding ADR is accepted.

## Coverage by decision area

{counts.to_markdown()}

## Sources by decision area

{chr(10).join(s + chr(10) * 2 for s in sections).rstrip()}

## Claims requiring own testing

External sources inform these claims but cannot settle them, because each one
depends on Aurora's data, workload, team, pricing or customers. All are **Open**
until the test runs and the result is recorded here.

{(chr(10) * 2).join(claim_blocks)}
"""


def main() -> int:
    sources = pd.read_csv(SOURCES_FILE, dtype=str)
    claims = pd.read_csv(CLAIMS_FILE, dtype=str)
    _, assumptions = load_assumptions()

    errors = validate(sources, claims, assumptions)
    if errors:
        print("Evidence validation failed:", *errors, sep="\n  - ", file=sys.stderr)
        return 1

    EVIDENCE_MD.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_MD.write_text(render(sources, claims), encoding="utf-8")
    print(
        f"Wrote {EVIDENCE_MD.relative_to(ROOT)}: {len(sources)} sources, "
        f"{len(claims)} own-testing claims"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
