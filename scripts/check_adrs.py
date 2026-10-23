"""Validate the ADRs in docs/adr against the evidence table and assumptions.

Checks, per ADR:
  * header fields present; `Decision area` matches the evidence table;
    `Depends on` only names lower-numbered ADRs that exist
  * required sections: Context, Options considered, Decision, Consequences
    (with a "Reversal cost" subsection), Human-review gates, Evidence carried
    forward, Sign-off
  * >= 2 options, exactly one marked "(chosen)"; every rejected option has a
    "Genuine case for it" backed by external evidence or an assumption, not only
    judgement (guards against strawman alternatives), and a "Case against"
  * every citation resolves: [E-xx-nn, label] must exist AND carry its labeled
    evidence type, [A-xxx-nn] and [C-xx-nn, own testing] must exist
  * every bullet in the argument sections carries at least one citation
  * the evidence table carries forward every source for the ADR's decision
    area, with matching labels, and every own-testing claim for the area is cited
  * sign-off table present; reports whether the spec gate is open

Exit status is non-zero if any check fails. The spec gate being closed
(pending sign-offs) is reported but is not a failure.

Usage:
    python scripts/check_adrs.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from build_assumptions import ROOT, load_assumptions
from build_evidence import CLAIMS_FILE, SOURCES_FILE

ADR_DIR = ROOT / "docs" / "adr"

REQUIRED_SECTIONS = (
    "Context",
    "Options considered",
    "Decision",
    "Consequences",
    "Human-review gates",
    "Evidence carried forward",
    "Sign-off",
)
# Bullets in these sections make substantive claims and must cite something.
CITED_SECTIONS = {
    "Context",
    "Options considered",
    "Decision",
    "Consequences",
    "Revisit triggers",
    "Supersede triggers",
}
SIGNOFF_DECISIONS = {"Pending", "Approved", "Approved with conditions", "Rejected"}

EVIDENCE_CITE = re.compile(r"\[(E-[A-Z]+-\d+), ([a-z -]+)\]")
ASSUMPTION_CITE = re.compile(r"\[(A-[A-Z]+-\d+)\]")
CLAIM_CITE = re.compile(r"\[(C-[A-Z]+-\d+), own testing\]")
ADR_CITE = re.compile(r"\[(ADR-\d{3})\]")
OTHER_CITE = re.compile(r"\[(projections|judgement)\]")
ANY_ID = re.compile(r"\b([EAC]-[A-Z]+-\d+)\b")


@dataclass
class Adr:
    path: Path
    number: str
    text: str
    header: dict[str, str]
    sections: dict[str, str]
    errors: list[str] = field(default_factory=list)


def parse(path: Path) -> Adr:
    text = path.read_text(encoding="utf-8")
    number = re.match(r"(ADR-\d{3})", path.name).group(1)
    header = dict(re.findall(r"^\*\*([^*]+):\*\*\s*(.+)$", text, flags=re.M))
    sections: dict[str, str] = {}
    for match in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S):
        title = re.sub(r"\s*\(.*\)$", "", match.group(1)).strip()
        sections[title] = match.group(2)
    return Adr(path, number, text, header, sections)


def has_citation(line: str) -> bool:
    return any(
        p.search(line) for p in (EVIDENCE_CITE, ASSUMPTION_CITE, CLAIM_CITE, ADR_CITE, OTHER_CITE)
    )


def check(adr: Adr, adrs: dict[str, Adr], sources: pd.DataFrame, claims: pd.DataFrame, assumptions: dict) -> None:
    err = adr.errors.append
    labels = dict(zip(sources["source_id"], sources["evidence_type"]))

    # header
    for key in ("Status", "Date", "Owner", "Decision area", "Depends on", "Door type"):
        if key not in adr.header:
            err(f"missing header field '{key}'")
    area = adr.header.get("Decision area", "")
    if area not in set(sources["decision_area"]):
        err(f"decision area '{area}' not in evidence table")
    for dep in re.findall(r"ADR-\d{3}", adr.header.get("Depends on", "")):
        if dep not in adrs:
            err(f"depends on missing {dep}")
        elif dep >= adr.number:
            err(f"depends on {dep}, which must be decided before {adr.number}")

    # sections
    for name in REQUIRED_SECTIONS:
        if name not in adr.sections:
            err(f"missing section '## {name}'")
    if "### Reversal cost" not in adr.sections.get("Consequences", ""):
        err("Consequences has no '### Reversal cost' subsection")

    # options: no strawmen
    options = re.split(r"^### ", adr.sections.get("Options considered", ""), flags=re.M)[1:]
    if len(options) < 2:
        err("fewer than 2 options considered")
    chosen = [o for o in options if "(chosen)" in o.splitlines()[0]]
    if len(chosen) != 1:
        err(f"expected exactly one '(chosen)' option, found {len(chosen)}")
    for opt in options:
        title = opt.splitlines()[0]
        cases_for = [l for l in opt.splitlines() if "**Genuine case for it:**" in l]
        if not any(EVIDENCE_CITE.search(l) or ASSUMPTION_CITE.search(l) for l in cases_for):
            err(f"option '{title}' has no evidence- or assumption-backed 'Genuine case for it'")
        if "**Case against:**" not in opt:
            err(f"option '{title}' has no 'Case against'")

    # citations resolve
    for sid, label in EVIDENCE_CITE.findall(adr.text):
        if sid not in labels:
            err(f"cites unknown evidence {sid}")
        elif labels[sid] != label:
            err(f"{sid} cited as '{label}' but labeled '{labels[sid]}' in evidence table")
    for aid in ASSUMPTION_CITE.findall(adr.text):
        if aid not in assumptions:
            err(f"cites unknown assumption {aid}")
    claim_ids = set(claims["claim_id"])
    for cid in CLAIM_CITE.findall(adr.text):
        if cid not in claim_ids:
            err(f"cites unknown claim {cid}")
    for ref in ADR_CITE.findall(adr.text):
        if ref not in adrs:
            err(f"cites unknown {ref}")

    # every substantive bullet is cited
    for name, body in adr.sections.items():
        if name not in CITED_SECTIONS:
            continue
        for line in body.splitlines():
            if re.match(r"^\s*(- |\d+\. )", line) and not has_citation(line):
                err(f"uncited claim in '{name}': {line.strip()[:70]}")

    # evidence carried forward
    table = adr.sections.get("Evidence carried forward", "")
    rows = dict(re.findall(r"^\| (E-[A-Z]+-\d+) \| ([^|]+?) \|", table, flags=re.M))
    for sid in sources.loc[sources["decision_area"] == area, "source_id"]:
        if sid not in rows:
            err(f"evidence table does not carry forward {sid}")
    for sid, label in rows.items():
        if sid in labels and not label.startswith(labels[sid]):
            err(f"evidence table lists {sid} as '{label}', expected '{labels[sid]}'")
    cited_claims = set(CLAIM_CITE.findall(adr.text))
    for cid in claims.loc[claims["decision_area"] == area, "claim_id"]:
        if cid not in cited_claims:
            err(f"own-testing claim {cid} for this area is never cited")

    # stray IDs written without the citation format
    formatted = set(EVIDENCE_CITE.findall(adr.text)) | set(rows.items())
    formatted_ids = {i for i, _ in formatted} | set(ASSUMPTION_CITE.findall(adr.text)) | cited_claims
    for stray in sorted(set(ANY_ID.findall(adr.text)) - formatted_ids):
        err(f"{stray} mentioned without a labeled citation")


def signoff_state(adr: Adr) -> tuple[int, int]:
    rows = []
    for line in adr.sections.get("Sign-off", "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and cells[0] != "Role" and not cells[0].startswith(":"):
            rows.append((cells[0], cells[2]))
    for role, decision in rows:
        if decision not in SIGNOFF_DECISIONS:
            adr.errors.append(f"sign-off for '{role}' has unknown decision '{decision}'")
    signed = sum(d.startswith("Approved") for _, d in rows)
    return signed, len(rows)


def main() -> int:
    sources = pd.read_csv(SOURCES_FILE, dtype=str)
    claims = pd.read_csv(CLAIMS_FILE, dtype=str)
    _, assumptions = load_assumptions()

    adrs = {a.number: a for a in map(parse, sorted(ADR_DIR.glob("ADR-*.md")))}
    if not adrs:
        print(f"No ADRs found in {ADR_DIR}", file=sys.stderr)
        return 1

    failed = False
    approved: dict[str, bool] = {}
    for number, adr in adrs.items():
        check(adr, adrs, sources, claims, assumptions)
        signed, total = signoff_state(adr)
        approved[number] = total > 0 and signed == total
        deps = re.findall(r"ADR-\d{3}", adr.header.get("Depends on", ""))
        if approved[number] and not all(approved.get(d) for d in deps):
            adr.errors.append("fully signed before its dependencies")

        gate = "OPEN" if approved[number] else f"BLOCKED ({signed}/{total} signed)"
        status = "FAIL" if adr.errors else "ok"
        print(f"{status:4}  {adr.path.name}  spec gate: {gate}")
        for e in adr.errors:
            print(f"      - {e}")
        failed |= bool(adr.errors)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
