"""Validate a dataset file against its data contract and governance rules.

Fails (non-zero exit) when any rule is violated. Rules, thresholds and failure
behaviors come from contracts/schemas/*.yaml; consent state comes from the
consent ledger; review gates must be named explicitly (unnamed = not cleared).

Exit codes: 0 PASS, 1 FAIL (rows quarantined), 2 FAIL (BLOCKED).

Examples:
    python scripts/validate_contract.py features.churn_features
    python scripts/validate_contract.py rag.transcript_chunks --gate SEC-G3
    python scripts/validate_contract.py raw.subscription_events --data my_batch.parquet --json result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora_contracts.governance import CheckContext  # noqa: E402
from aurora_contracts.loader import load as load_contracts  # noqa: E402
from aurora_contracts.validator import format_report, validate  # noqa: E402

SYNTHETIC = ROOT / "synthetic" / "data"
CONSENT_COLUMNS = ["analytics_objection", "ai_processing", "deletion_pending", "health_data"]


def load_consent(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    return df.set_index("subscriber_id")[[c for c in CONSENT_COLUMNS if c in df.columns]]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Validate a dataset against its data contract.")
    ap.add_argument("dataset", help="contract dataset name, e.g. features.churn_features")
    ap.add_argument("--data", type=Path, help="Parquet file (default: synthetic/data/<dataset>.parquet)")
    ap.add_argument("--consent", type=Path, default=SYNTHETIC / "sources" / "consent.ledger.parquet")
    ap.add_argument("--gate", action="append", default=[], help="review gate cleared (repeatable), e.g. SEC-G3")
    ap.add_argument("--now", help="check time, ISO 8601 with offset (default: current time)")
    ap.add_argument("--tz", default="America/Chicago", help="local zone for daily deadlines")
    ap.add_argument("--consecutive-misses", type=int, default=0, help="prior consecutive freshness misses")
    ap.add_argument("--fail-on", choices=["any", "blocking"], default="any")
    ap.add_argument("--json", type=Path, help="write the machine-readable result here")
    ap.add_argument("--quarantine-out", type=Path, help="write quarantined rows here (Parquet)")
    return ap.parse_args(argv)


def run(args: argparse.Namespace, df: pd.DataFrame | None = None, out=sys.stdout) -> int:
    contracts = load_contracts()
    if args.dataset not in contracts.datasets:
        print(f"Unknown dataset {args.dataset}; known: {', '.join(contracts.datasets)}", file=out)
        return 2
    ds = contracts.datasets[args.dataset]
    if df is None:
        df = pd.read_parquet(args.data or SYNTHETIC / f"{args.dataset}.parquet")
    zone = ZoneInfo(args.tz)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(zone)
    misses = {r.id: args.consecutive_misses for r in ds.governance if r.rule == "freshness"}
    ctx = CheckContext(now=now, local_tz=zone, consent=load_consent(args.consent),
                       gates_cleared=frozenset(args.gate), consecutive_failures=misses)

    result = validate(df, ds, ctx, fail_on=args.fail_on)
    keys = {"raw.subscription_events": ["event_id", "event_type"],
            "rag.transcript_chunks": ["chunk_id", "ticket_id", "subscriber_id"]}.get(ds.name, ds.primary_key)
    print(format_report(result, keys), file=out)
    if args.json:
        args.json.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    if args.quarantine_out and len(result.outcome.quarantined):
        result.outcome.quarantined.to_parquet(args.quarantine_out, index=False)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
