"""Generate synthetic Aurora data that conforms to the contracts, with seeded edge cases.

Simulates the source systems (driven by the assumptions register), injects the
edge cases in aurora_synthetic/edge_cases.py, builds the five contract datasets
and writes:

  synthetic/data/<dataset>.parquet          contract datasets (column order and types per contract)
  synthetic/data/sources/<system.table>.parquet   simulated source-system tables (lineage sources)
  synthetic/data/producer_payloads.jsonl    sample producer payloads for JSON Schema validation
  synthetic/data/manifest.json              config, edge cases, row counts, model report
  synthetic/data/_truth.parquet             simulation ground truth (validation only; not a source)

All personal data is fictitious. Output is deterministic for a given seed.

Usage:
    python scripts/generate_synthetic.py [--subscribers 600] [--days 150] [--seed 660]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora_contracts.loader import load as load_contracts  # noqa: E402
from aurora_synthetic import edge_cases  # noqa: E402
from aurora_synthetic.simulate import Config, simulate  # noqa: E402
from aurora_synthetic.transform import build_all  # noqa: E402
from build_assumptions import load_assumptions  # noqa: E402

OUT = ROOT / "synthetic" / "data"
TRUTH_COLS = ["start_day", "cancel_day", "involuntary", "propensity"]


def _ts(df: pd.DataFrame) -> pd.DataFrame:
    """Python datetimes -> UTC pandas timestamps so Parquet stores real timestamps."""
    out = df.copy()
    for c in out.columns:
        if out[c].map(lambda v: hasattr(v, "tzinfo") and hasattr(v, "hour")).any():
            out[c] = pd.to_datetime(out[c], utc=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--subscribers", type=int, default=600)
    ap.add_argument("--days", type=int, default=150, help="history length; >= 140 so 90-day windows and two matured 30-day label dates fit")
    ap.add_argument("--seed", type=int, default=660)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    if args.days < 140:
        ap.error("--days must be >= 140")

    _, assumptions = load_assumptions()
    contracts = load_contracts()
    cfg = Config.from_assumptions(assumptions, subscribers=args.subscribers, days=args.days, seed=args.seed)

    world = simulate(cfg)
    cases = edge_cases.seed(world)
    built = build_all(world, contracts)

    out: Path = args.out
    (out / "sources").mkdir(parents=True, exist_ok=True)
    for name, df in built.datasets.items():
        df.to_parquet(out / f"{name}.parquet", index=False)
    sources = {
        "producers.events": world.events.drop(columns=["ingested_at"]),
        "subscription_platform.subscribers": world.subscribers.drop(columns=TRUTH_COLS),
        "commerce.orders": world.orders,
        "billing.payments": world.payments,
        "consent.ledger": world.consent,
        "helpdesk.tickets": world.tickets.drop(columns=["day"]),
    }
    for name, df in sources.items():
        _ts(df).to_parquet(out / "sources" / f"{name}.parquet", index=False)
    world.subscribers[["subscriber_id", *TRUTH_COLS]].to_parquet(out / "_truth.parquet", index=False)
    with (out / "producer_payloads.jsonl").open("w", encoding="utf-8") as fh:
        for p in built.producer_payloads:
            fh.write(json.dumps(p) + "\n")

    manifest = {
        "config": {k: str(v) for k, v in vars(cfg).items()},
        "build_time": world.notes["build_time"].isoformat(),
        "promo_day": cfg.promo_day,
        "row_counts": {**{k: len(v) for k, v in built.datasets.items()}, **{f"sources/{k}": len(v) for k, v in sources.items()}},
        "hourly_cells": {"total": world.notes["hourly_cells_total"],
                         "below_k_before_rollup": world.notes["hourly_cells_suppressed_or_dropped"]},
        "edge_cases": [c.to_dict() for c in cases],
        "model_report": built.model_report,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    print(f"Generated {cfg.subscribers} subscribers x {cfg.days} days (seed {cfg.seed}) -> {out}")
    for k, v in manifest["row_counts"].items():
        print(f"  {k:48} {v:>10,}")
    print(f"  seeded edge cases: {', '.join(c.id for c in cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
