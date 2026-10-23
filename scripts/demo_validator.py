"""Demonstrate the contract validator against the seeded bad records.

For every case in validator/demo_cases.yaml: build the batch (clean baseline +
the named seeded records from synthetic/data), write it to
synthetic/data/demo/<case>.parquet, run scripts/validate_contract.py on it,
and compare the actual verdict, exit code, failed rules and quarantined rows
with the expected output declared in the case file.

Writes docs/validator_demo.md (expected vs actual + full validator output for
each run). Exits non-zero if any case does not match its expectation.

Usage:
    python scripts/generate_synthetic.py   # once, if synthetic/data is missing
    python scripts/demo_validator.py
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import validate_contract  # noqa: E402

CASES = ROOT / "validator" / "demo_cases.yaml"
DOC = ROOT / "docs" / "validator_demo.md"

# Which column identifies the seeded rows of each edge case, per dataset.
SELECTOR = {"EC-02": "ticket_id", "EC-03": "ticket_id", "EC-05": "event_id", "EC-08": "event_id", "EC-09": "event_id"}
SEEDED_BY_DATASET = {
    "raw.subscription_events": ["EC-05", "EC-08", "EC-09"],
    "marts.retention_subscription_health_daily": ["EC-01", "EC-04", "EC-13"],
    "features.churn_features": ["EC-01", "EC-04", "EC-13"],
    "rag.transcript_chunks": ["EC-02", "EC-03", "EC-04", "EC-10", "EC-13"],
}


class Demo:
    def __init__(self, data: Path, doc: Path = DOC):
        self.data, self.doc = data, doc
        self.manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
        self.ec = {c["id"]: c for c in self.manifest["edge_cases"]}
        self.zone = ZoneInfo(self.manifest["config"]["tz"])
        self.build_date = datetime.fromisoformat(self.manifest["build_time"]).date()
        self.out_dir = data / "demo"
        self.out_dir.mkdir(exist_ok=True)

    def mask(self, df: pd.DataFrame, ec_id: str) -> pd.Series:
        col = SELECTOR.get(ec_id, "subscriber_id")
        return df[col] == self.ec[ec_id]["keys"][col]

    def batch(self, dataset: str, inject: list[str]) -> tuple[pd.DataFrame, pd.Series]:
        full = pd.read_parquet(self.data / f"{dataset}.parquet")
        seeded_any = pd.Series(False, index=full.index)
        for ec in SEEDED_BY_DATASET[dataset]:
            seeded_any |= self.mask(full, ec)
        base = full[~seeded_any]
        if dataset == "raw.subscription_events":
            base = base[base["ingested_at"] >= base["ingested_at"].max() - pd.Timedelta(hours=24)]
        injected = pd.concat([full[self.mask(full, ec)] for ec in inject]) if inject else full.iloc[0:0]
        df = pd.concat([base, injected]).reset_index(drop=True)
        is_seeded = pd.Series([False] * len(base) + [True] * len(injected))
        return df, is_seeded

    def now(self, dataset: str, df: pd.DataFrame) -> str:
        if dataset == "raw.subscription_events":
            return (df["ingested_at"].max() + pd.Timedelta(minutes=5)).isoformat()
        return datetime.combine(self.build_date, time(8), self.zone).isoformat()

    def run_case(self, case: dict) -> dict:
        ds = case["dataset"]
        df, is_seeded = self.batch(ds, case.get("inject", []))
        path = self.out_dir / f"{case['id']}.parquet"
        df.to_parquet(path, index=False)
        result_path = self.out_dir / f"{case['id']}.json"
        argv = [ds, "--data", str(path), "--consent", str(self.data / "sources" / "consent.ledger.parquet"),
                "--now", self.now(ds, df), "--json", str(result_path), "--fail-on", case.get("fail_on", "any")]
        for g in case.get("gates", []):
            argv += ["--gate", g]
        buf = io.StringIO()
        code = validate_contract.run(validate_contract.parse_args(argv), out=buf)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        failed = {r["rule"]: r["behavior"] for r in result["rules"] if not r["passed"]}

        # Quarantined rows vs the seeded rows that were injected.
        exp = case["expected"]
        n_q = result["rows_quarantined"]
        if exp["quarantined"] == "none":
            q_ok, q_expected = n_q == 0, "0 rows"
        elif exp["quarantined"] == "seeded_duplicate":
            q_ok, q_expected = n_q == int(is_seeded.sum()) - 1, f"{int(is_seeded.sum()) - 1} row (second copy)"
        else:
            q_ok, q_expected = n_q == int(is_seeded.sum()), f"{int(is_seeded.sum())} seeded rows"
        cmd = "python scripts/validate_contract.py " + " ".join(
            a if not a.startswith(str(ROOT)) else Path(a).relative_to(ROOT).as_posix() for a in argv
            if not a.endswith(".json") and a != "--json")
        actual = {"verdict": result["verdict"], "exit_code": code, "failed_rules": failed, "quarantined": f"{n_q} rows"}
        match = (actual["verdict"] == exp["verdict"] and code == exp["exit_code"]
                 and failed == (exp["failed_rules"] or {}) and q_ok)
        return {"case": case, "rows": len(df), "seeded_rows": int(is_seeded.sum()), "cmd": cmd, "output": buf.getvalue(),
                "actual": actual, "q_expected": q_expected, "match": match}

    def render(self, runs: list[dict]) -> str:
        table = pd.DataFrame([{
            "Case": r["case"]["id"], "Batch": r["case"]["title"], "Dataset": r["case"]["dataset"],
            "Rows (seeded)": f"{r['rows']:,} ({r['seeded_rows']})",
            "Expected": f"{r['case']['expected']['verdict']} / exit {r['case']['expected']['exit_code']} / "
                        f"{_rules(r['case']['expected']['failed_rules'])} / quarantine {r['q_expected']}",
            "Actual": f"{r['actual']['verdict']} / exit {r['actual']['exit_code']} / {_rules(r['actual']['failed_rules'])} / "
                      f"quarantine {r['actual']['quarantined']}",
            "Match": "✓" if r["match"] else "**✗**",
        } for r in runs])
        transcripts = "\n\n".join(
            f"### {r['case']['id']} — {r['case']['title']}\n\n```console\n$ {r['cmd']}\n{r['output'].rstrip()}\n"
            f"$ echo $?\n{r['actual']['exit_code']}\n```" for r in runs)
        ok = sum(r["match"] for r in runs)
        return f"""# Contract Validator — Demonstration Run

_Generated by `scripts/demo_validator.py` from `validator/demo_cases.yaml` and the synthetic
data in `synthetic/data/` (seed {self.manifest['config']['seed']}, build {self.manifest['build_time']}).
Do not edit by hand._

The validator (`scripts/validate_contract.py`, `aurora_contracts/validator.py`) adds no rules
of its own. It runs each dataset's governance rules from `contracts/schemas/*.yaml` with the
failure behaviors declared there, and turns the outcome into an exit code the orchestrator
can gate on (ADR-002):

| Exit | Verdict | Meaning |
|:--|:--|:--|
| 0 | PASS | Every rule passed (or, with `--fail-on blocking`, only non-blocking quarantine/warn) |
| 1 | FAIL | Violations found; offending rows quarantined, the rest publishable |
| 2 | FAIL (BLOCKED) | A blocking rule failed (`block_publish` or `security_incident`); nothing publishes |

Each batch is a clean baseline (every seeded bad record removed) plus the seeded records
named in the case. Expected outcomes were written into the case file from the contracts
before the run.

**Result: {ok}/{len(runs)} cases match the expected output.**

## Expected vs actual

{table.to_markdown(index=False)}

## Validator output

{transcripts}
"""

    def run(self) -> int:
        cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
        runs = [self.run_case(c) for c in cases]
        self.doc.write_text(self.render(runs), encoding="utf-8")
        for r in runs:
            a, e = r["actual"], r["case"]["expected"]
            print(f"{'ok  ' if r['match'] else 'MISS'} {r['case']['id']:<6} expected {e['verdict']:<15} exit {e['exit_code']}  "
                  f"actual {a['verdict']:<15} exit {a['exit_code']}  {_rules(a['failed_rules'])}")
        bad = [r for r in runs if not r["match"]]
        print(f"{len(runs) - len(bad)}/{len(runs)} cases match. Wrote {self.doc.name}")
        return 1 if bad else 0


def _rules(rules: dict) -> str:
    return ", ".join(f"{k} -> {v}" for k, v in rules.items()) or "no rule failures"


if __name__ == "__main__":
    data = ROOT / "synthetic" / "data"
    if not (data / "manifest.json").exists():
        sys.exit("No synthetic data found; run scripts/generate_synthetic.py first.")
    sys.exit(Demo(data).run())
