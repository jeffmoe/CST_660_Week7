"""Build the assumptions register and derived volume/storage projections.

Reads assumptions/assumptions.yaml, projects subscribers, event volume,
transcript volume, storage footprint and LLM spend month-by-month for the
low / base / high scenarios, and writes:

  assumptions/derived/projections.csv   month x scenario projections
  docs/assumptions_register.md          human-readable register

Usage:
    python scripts/build_assumptions.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
ASSUMPTIONS_FILE = ROOT / "assumptions" / "assumptions.yaml"
PROJECTIONS_FILE = ROOT / "assumptions" / "derived" / "projections.csv"
REGISTER_FILE = ROOT / "docs" / "assumptions_register.md"

SCENARIOS = ("low", "base", "high")

# For these assumptions a *smaller* value produces a *larger* footprint, so the
# high (worst-case) scenario takes the low value and vice versa.
INVERTED = {"A-STO-01", "A-TRN-04"}

BYTES_PER_TOKEN = 4  # rough UTF-8 English average
BYTES_PER_FLOAT32 = 4
GB = 1024**3


def load_assumptions(path: Path = ASSUMPTIONS_FILE) -> tuple[dict, dict[str, dict]]:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    by_id = {a["id"]: a for a in doc["assumptions"]}
    if len(by_id) != len(doc["assumptions"]):
        raise ValueError("Duplicate assumption ids in register")
    return doc["meta"], by_id


def value(a: dict[str, dict], aid: str, scenario: str) -> float:
    if aid in INVERTED:
        scenario = {"low": "high", "high": "low"}.get(scenario, scenario)
    return a[aid][scenario]


def project(meta: dict, a: dict[str, dict]) -> pd.DataFrame:
    months = np.arange(meta["planning_horizon_months"] + 1)
    horizon = meta["planning_horizon_months"]
    budget_alloc = a["A-BUD-02"]["allocation"]
    frames = []

    for s in SCENARIOS:
        start, end = value(a, "A-GRW-01", s), value(a, "A-GRW-02", s)
        subs = start * (end / start) ** (months / horizon)

        events_per_sub = (
            value(a, "A-EVT-01", s) * value(a, "A-EVT-02", s) + value(a, "A-EVT-03", s)
        )
        events_day = subs * events_per_sub
        events_peak = events_day * value(a, "A-EVT-04", s)
        raw_gb_day = events_day * value(a, "A-EVT-05", s) / GB
        stored_gb_day = raw_gb_day / value(a, "A-STO-01", s)

        # Month 0 is the starting snapshot; volume accrues from month 1 onward.
        days_in_month = np.where(months == 0, 0, 30.4)
        retention_days = value(a, "A-STO-02", s) * 30.4
        cum_event_gb = np.minimum(
            np.cumsum(stored_gb_day * days_in_month),
            stored_gb_day * retention_days,
        )
        modeled_gb = cum_event_gb * value(a, "A-STO-03", s)

        transcripts_month = np.where(months == 0, 0, subs * value(a, "A-TRN-01", s))
        cum_transcripts = value(a, "A-TRN-03", s) + np.cumsum(transcripts_month)
        tokens = value(a, "A-TRN-02", s)
        chunks_per_transcript = math.ceil(tokens / value(a, "A-TRN-04", s))
        vectors = cum_transcripts * chunks_per_transcript
        transcript_text_gb = cum_transcripts * tokens * BYTES_PER_TOKEN / GB
        vector_gb = (
            vectors
            * value(a, "A-TRN-05", s)
            * BYTES_PER_FLOAT32
            * value(a, "A-STO-04", s)
            / GB
        )

        # Assistant spend assumes one assistant conversation per support contact.
        llm_cost_month = transcripts_month * value(a, "A-BUD-03", s)
        llm_budget_month = (
            a["A-BUD-01"]["base"] * budget_alloc["llm_inference_and_embeddings"] / 12
        )

        frames.append(
            pd.DataFrame(
                {
                    "scenario": s,
                    "month": months,
                    "subscribers": subs.round(0),
                    "events_per_day": events_day.round(0),
                    "events_peak_day": events_peak.round(0),
                    "events_per_sec_peak_avg": (events_peak / 86_400).round(1),
                    "raw_event_gb_per_day": raw_gb_day.round(3),
                    "stored_event_gb_cumulative": cum_event_gb.round(1),
                    "modeled_layer_gb": modeled_gb.round(1),
                    "transcripts_per_month": transcripts_month.round(0),
                    "transcripts_cumulative": cum_transcripts.round(0),
                    "vectors_cumulative": vectors.round(0),
                    "transcript_text_gb": transcript_text_gb.round(2),
                    "vector_index_gb": vector_gb.round(2),
                    "total_storage_gb": (
                        cum_event_gb + modeled_gb + transcript_text_gb + vector_gb
                    ).round(1),
                    "assistant_llm_cost_usd_month": llm_cost_month.round(0),
                    "assistant_llm_budget_usd_month": round(llm_budget_month, 0),
                }
            )
        )

    return pd.concat(frames, ignore_index=True)


def fmt_value(row: dict) -> tuple[str, str, str]:
    if "allocation" in row:
        split = ", ".join(f"{k} {v:.0%}" for k, v in row["allocation"].items())
        return (split, "", "")
    return tuple(f"{row[s]:,}" if isinstance(row[s], (int, float)) else str(row[s]) for s in SCENARIOS)


def render_register(meta: dict, a: dict[str, dict], proj: pd.DataFrame) -> str:
    alloc_total = sum(a["A-BUD-02"]["allocation"].values())
    if not math.isclose(alloc_total, 1.0):
        raise ValueError(f"A-BUD-02 allocation sums to {alloc_total}, expected 1.0")

    rows = []
    for aid, row in a.items():
        low, base, high = fmt_value(row)
        rows.append(
            {
                "ID": aid,
                "Category": row["category"],
                "Assumption": row["name"],
                "Unit": row["unit"],
                "Low": low,
                "Base": base,
                "High": high,
                "Confidence": row["confidence"],
                "Owner": row["owner"],
                "Own testing?": "**YES**" if row["own_testing"] else "no",
                "Jurisdiction assumption?": "**⚑ YES**" if row.get("jurisdiction_assumption") else "no",
            }
        )
    register = pd.DataFrame(rows)

    detail = "\n".join(
        f"- **{aid} {row['name']}** — {' '.join(row['basis'].split())} "
        f"*Validation:* {row['validation']}"
        for aid, row in a.items()
    )

    end = proj[proj["month"] == proj["month"].max()].set_index("scenario")
    summary_cols = {
        "subscribers": "Subscribers",
        "events_per_day": "Events / day",
        "events_peak_day": "Peak events / day",
        "events_per_sec_peak_avg": "Peak-day events / sec (avg)",
        "raw_event_gb_per_day": "Raw event GB / day",
        "transcripts_per_month": "Transcripts / month",
        "vectors_cumulative": "Vectors (cumulative)",
        "total_storage_gb": "Total stored GB",
        "assistant_llm_cost_usd_month": "Assistant LLM $ / month",
        "assistant_llm_budget_usd_month": "LLM budget $ / month",
    }
    summary = end[list(summary_cols)].rename(columns=summary_cols).T
    summary = summary.map(lambda v: f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}")

    over_budget = end[
        end["assistant_llm_cost_usd_month"] > end["assistant_llm_budget_usd_month"]
    ].index.tolist()
    budget_note = (
        f"> **Budget flag:** month-{int(end['month'].iloc[0])} assistant LLM spend exceeds "
        f"its allocation in scenario(s): {', '.join(over_budget)}. "
        "Validate A-BUD-03 (C-AI-04) before committing to the allocation."
        if over_budget
        else "> Assistant LLM spend stays within its allocation in all scenarios."
    )

    flagged = register[register["Own testing?"] == "**YES**"][["ID", "Assumption", "Owner"]]
    jurisdiction = register[register["Jurisdiction assumption?"] != "no"][
        ["ID", "Assumption", "Low", "Base", "High", "Owner"]
    ]

    return f"""# Assumptions Register — Subscription Health Data Product

_Generated by `scripts/build_assumptions.py` from `assumptions/assumptions.yaml`
(register v{meta['register_version']}, last reviewed {meta['last_reviewed']}). Do not edit by hand._

Planning horizon: {meta['planning_horizon_months']} months from {meta['fiscal_year_start']}.
Scenarios: **low / base / high**. Platform sizing decisions use **high**; budget
tracking uses **base**. For compression ratio and chunk size the worst case is the
*low* value, so the high scenario uses it.

## Register

{register.to_markdown(index=False)}

## Basis and validation

{detail}

## Month-{int(end['month'].iloc[0])} projections by scenario

{summary.to_markdown()}

{budget_note}

Full month-by-month projections: `assumptions/derived/projections.csv`.

## Assumptions that require own testing

These values cannot be confirmed from external sources; they must be measured on
Aurora's own data or workloads. Each maps to a claim in
`evidence/own_testing_claims.csv`.

{flagged.to_markdown(index=False)}

## Jurisdiction assumptions (⚑ Legal must confirm)

These set the regulatory scope. `scripts/build_security_matrix.py` resolves which
regimes apply, and therefore which retention windows hold, from these values per
scenario; changing one changes `docs/security_matrix.md`.

{jurisdiction.to_markdown(index=False)}
"""


def main() -> None:
    meta, a = load_assumptions()
    proj = project(meta, a)

    PROJECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    proj.to_csv(PROJECTIONS_FILE, index=False)

    REGISTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTER_FILE.write_text(render_register(meta, a, proj), encoding="utf-8")

    print(f"Wrote {PROJECTIONS_FILE.relative_to(ROOT)} ({len(proj)} rows)")
    print(f"Wrote {REGISTER_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
