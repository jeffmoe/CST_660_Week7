"""Cost-aware operating plan: monthly run-rate by component and scenario.

Every input is an assumption ID or a projection; nothing is hard-coded here.
The ingestion line items follow the pattern decided in ADR-003 (micro-batch),
with streaming priced alongside for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

SCENARIOS = ("low", "base", "high")


@dataclass(frozen=True)
class Inputs:
    a: dict[str, dict]  # assumptions by id
    proj: pd.DataFrame  # assumptions/derived/projections.csv
    cfg: dict  # mlops/cost_model.yaml


def _v(i: Inputs, aid: str, s: str):
    return i.a[aid][s]


def conversation_cost(i: Inputs, s: str, model: str | None = None) -> float:
    """USD per assistant conversation from token assumptions and the price book."""
    m = i.cfg["price_book"]["models"][model or _v(i, "A-LLM-05", s)]
    mult = i.cfg["price_book"]["cache_write_multiplier"]
    w = i.cfg["llm"]["cached_tokens_written_share"]
    tin, tout, cached = _v(i, "A-LLM-02", s), _v(i, "A-LLM-03", s), _v(i, "A-LLM-04", s)
    uncached_in = tin * (1 - cached) * m["input"]
    cached_in = tin * cached * ((1 - w) * m["cache_read"] + w * mult * m["input"])
    return (uncached_in + cached_in + tout * m["output"]) / 1e6


def line_items(i: Inputs, s: str, month: int, model: str | None = None, pattern: str | None = None) -> dict[str, float]:
    row = i.proj[(i.proj["scenario"] == s) & (i.proj["month"] == month)].iloc[0]
    days = i.cfg["meta"]["days_per_month"]
    ing = i.cfg["ingestion"]
    ev = i.cfg["evaluation_workload"]
    price = _v(i, "A-UC-01", s)
    model_id = model or _v(i, "A-LLM-05", s)
    m = i.cfg["price_book"]["models"][model_id]
    pattern = pattern or ing["pattern"]

    loads_per_day = 24 * 60 / ing["landing_minutes"]
    conversations = row["transcripts_per_month"] * _v(i, "A-LLM-01", s)
    per_conv = conversation_cost(i, s, model_id)

    eval_answers = ev["smoke_questions_per_night"] * days + ev["full_suite_questions"] * ev["full_suite_trials"] * ev[
        "full_suite_runs_per_month"
    ] + ev["red_team_prompts_per_month"]
    judge_cost = (ev["judge_input_tokens"] * m["input"] + ev["judge_output_tokens"] * m["output"]) / 1e6

    emb_price = _v(i, "A-UC-09", s) / 1e6
    tokens_per_transcript = _v(i, "A-TRN-02", s)
    new_tokens = row["transcripts_per_month"] * tokens_per_transcript
    reindex_tokens = row["transcripts_cumulative"] * tokens_per_transcript * i.cfg["llm"]["reindex_full_corpus_per_year"] / 12
    query_tokens = conversations * i.cfg["llm"]["query_embedding_tokens_per_conversation"]

    items = {
        "micro_batch_loads": loads_per_day * _v(i, "A-UC-02", s) / 60 * price * days,
        "hourly_incremental_models": ing["hourly_incremental_runs_per_day"] * _v(i, "A-UC-03", s) / 60 * price * days,
        "nightly_build_and_scoring": _v(i, "A-UC-04", s) * price * days,
        "dashboards_and_adhoc": _v(i, "A-UC-05", s) * price * days,
        "churn_model_training": _v(i, "A-UC-12", s),
        "storage": row["total_storage_gb"] / 1024 * _v(i, "A-UC-06", s),
        "event_collection": row["events_per_day"] * days / 1e6 * _v(i, "A-UC-07", s),
        "orchestrator": _v(i, "A-UC-08", s),
        "assistant_inference": conversations * per_conv,
        "rag_evaluation": eval_answers * (per_conv + judge_cost),
        "embeddings": (new_tokens + reindex_tokens + query_tokens) * emb_price,
        "vector_store": _v(i, "A-UC-10", s),
        "observability": _v(i, "A-UC-11", s),
    }
    if pattern == "streaming":
        for k in ing["alternative"]["replaces"]:
            items[k] = 0.0
        items["streaming_stack"] = _v(i, ing["alternative"]["adds"], s)
    return items


def by_allocation(i: Inputs, items: dict[str, float]) -> pd.DataFrame:
    comp = {**i.cfg["components"], "streaming_stack": "ingestion_and_streaming"}
    alloc = i.a["A-BUD-02"]["allocation"]
    ceiling = i.a["A-BUD-01"]["base"] / 12
    df = pd.Series(items).groupby(comp).sum().reindex(alloc.keys(), fill_value=0.0).to_frame("estimate")
    df["allocation"] = [alloc[k] * ceiling for k in df.index]
    df["share_of_allocation"] = df["estimate"] / df["allocation"]
    return df


def monthly_series(i: Inputs, s: str) -> pd.DataFrame:
    months = sorted(m for m in i.proj["month"].unique() if m > 0)
    rows = [{"month": m, **line_items(i, s, m)} for m in months]
    df = pd.DataFrame(rows).set_index("month")
    df["total"] = df.sum(axis=1)
    return df
