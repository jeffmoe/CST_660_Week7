"""Chunking, evaluation metrics, drift detection, gates and the cost model."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
import yaml

from aurora_mlops import chunking, cost, drift, metrics
from aurora_mlops.gates import release_gate
from aurora_contracts.loader import ROOT


@pytest.fixture(scope="module")
def assumptions():
    with (ROOT / "assumptions" / "assumptions.yaml").open(encoding="utf-8") as fh:
        return {a["id"]: a for a in yaml.safe_load(fh)["assumptions"]}


@pytest.fixture(scope="module")
def inputs(assumptions):
    with (ROOT / "mlops" / "cost_model.yaml").open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cost.Inputs(assumptions, pd.read_csv(ROOT / "assumptions" / "derived" / "projections.csv"), cfg)


# ------------------------------------------------------------------ chunking
TRANSCRIPT = """Customer: Hi, I want to pause my subscription for two months.
Agent: I can help with that. Would you prefer to skip the next delivery instead?
Customer: No, a pause is better. Also my last box arrived damaged.
Agent: Sorry about that. I have issued a replacement at no charge.
Thank you for contacting Aurora Nutrition, we are here to help."""


def test_chunks_keep_turns_whole_and_strip_boilerplate():
    chunks = chunking.chunk_transcript("t1", TRANSCRIPT, "r1", max_tokens=40, min_tokens=5)
    assert len(chunks) > 1
    assert all(c.token_count <= 40 for c in chunks)
    assert all(not c.text.lower().endswith("here to help.") for c in chunks)
    for c in chunks:
        assert c.text.startswith(("Customer:", "Agent:"))


def test_consecutive_chunks_overlap_by_one_turn():
    chunks = chunking.chunk_transcript("t1", TRANSCRIPT, "r1", max_tokens=40, min_tokens=5)
    last_turn = chunks[0].text.split("Agent:")[-1].strip()
    assert last_turn in chunks[1].text


def test_chunk_ids_are_deterministic_and_versioned():
    a = chunking.chunk_transcript("t1", TRANSCRIPT, "r1")
    b = chunking.chunk_transcript("t1", TRANSCRIPT, "r1")
    c = chunking.chunk_transcript("t1", TRANSCRIPT, "r2")
    assert [x.chunk_id for x in a] == [x.chunk_id for x in b] != [x.chunk_id for x in c]


def test_over_long_turn_is_split_on_sentences():
    long_turn = "Customer: " + " ".join(f"Sentence number {i} about my order." for i in range(200))
    chunks = chunking.chunk_transcript("t2", long_turn, "r1", max_tokens=512)
    assert len(chunks) > 1 and all(c.token_count <= 512 for c in chunks)


# ------------------------------------------------------------------- metrics
def test_retrieval_metrics_known_values():
    retrieved, relevant = ["a", "x", "b", "y", "z"], {"a", "b"}
    assert metrics.recall_at_k(retrieved, relevant, 5) == 1.0
    assert metrics.recall_at_k(retrieved, relevant, 1) == 0.5
    assert metrics.reciprocal_rank(["x", "a"], relevant) == 0.5
    ideal = 1 + 1 / math.log2(3)
    assert metrics.ndcg_at_k(retrieved, relevant, 10) == pytest.approx((1 + 1 / math.log2(4)) / ideal)
    assert metrics.ndcg_at_k(["x"], {"a"}, 10) == 0.0


def test_churn_kpis_reward_a_good_ranking():
    rng = np.random.default_rng(0)
    y = rng.binomial(1, 0.07, 5000)
    good = np.clip(0.05 + 0.6 * y + rng.normal(0, 0.1, 5000), 0, 1)
    k = metrics.churn_model_kpis(y, good)
    assert k["roc_auc"] > 0.95 and k["lift_top_decile"] > 3
    noise = metrics.churn_model_kpis(y, rng.uniform(size=5000))
    assert noise["roc_auc"] == pytest.approx(0.5, abs=0.05)


def test_pass_hat_k_requires_every_trial():
    assert metrics.pass_hat_k([[True] * 4, [True, True, False, True]], 4) == 0.5


# --------------------------------------------------------------------- drift
def test_psi_bands():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 10_000)
    assert drift.psi_status(drift.psi(ref, rng.normal(0, 1, 10_000))) == "ok"
    assert drift.psi_status(drift.psi(ref, rng.normal(1, 1, 10_000))) == "act"


def test_similarity_shift_and_topic_divergence():
    rng = np.random.default_rng(2)
    base = rng.normal(0.8, 0.05, 2000)
    assert drift.similarity_shift(base, rng.normal(0.8, 0.05, 2000)).status == "ok"
    assert drift.similarity_shift(base, rng.normal(0.7, 0.05, 2000)).status == "act"
    same = {"billing": 0.4, "shipping": 0.4, "cancel": 0.2}
    assert drift.topic_divergence(same, same).status == "ok"
    assert drift.topic_divergence(same, {"billing": 0.1, "recall": 0.9}).status == "act"


def test_low_similarity_rate():
    assert drift.low_similarity_rate([0.2] * 20 + [0.9] * 80, floor=0.5).status == "act"


# --------------------------------------------------------------------- gates
def test_release_gate_fails_closed_on_missing_metric():
    thr = {"recall@5": {"min": 0.85}, "pii_leak_count": {"max": 0}}
    assert release_gate({"recall@5": 0.9, "pii_leak_count": 0}, thr) == []
    assert release_gate({"recall@5": 0.8, "pii_leak_count": 1}, thr) == ["recall@5: 0.8 < 0.85", "pii_leak_count: 1 > 0"]
    assert release_gate({"pii_leak_count": 0}, thr) == ["recall@5: not measured"]


# ---------------------------------------------------------------------- cost
def test_conversation_cost_matches_hand_calculation(inputs):
    # Opus 5.5 base: 8k in (30% cached, 5% of that re-written), 1k out.
    expected = (8000 * 0.7 * 4.0 + 8000 * 0.3 * (0.95 * 0.20 + 0.05 * 1.25 * 4.0) + 1000 * 20.0) / 1e6
    assert cost.conversation_cost(inputs, "base") == pytest.approx(expected)


def test_assistant_cost_scales_with_conversations_per_contact(inputs, assumptions):
    base = cost.line_items(inputs, "base", 12)["assistant_inference"]
    doubled = dict(assumptions)
    doubled["A-LLM-01"] = {**assumptions["A-LLM-01"], "base": 2.0}
    more = cost.line_items(cost.Inputs(doubled, inputs.proj, inputs.cfg), "base", 12)["assistant_inference"]
    assert more == pytest.approx(2 * base)


def test_streaming_alternative_replaces_micro_batch_loads(inputs, assumptions):
    mb = cost.line_items(inputs, "base", 12)
    st = cost.line_items(inputs, "base", 12, pattern="streaming")
    assert st["micro_batch_loads"] == 0
    assert sum(st.values()) - sum(mb.values()) == pytest.approx(assumptions["A-UC-13"]["base"] - mb["micro_batch_loads"])


def test_allocation_rollup_covers_every_budget_line(inputs, assumptions):
    df = cost.by_allocation(inputs, cost.line_items(inputs, "base", 12))
    assert list(df.index) == list(assumptions["A-BUD-02"]["allocation"])
    assert df["estimate"].sum() == pytest.approx(sum(cost.line_items(inputs, "base", 12).values()))


def test_build_coupling_checks_pass():
    import build_mlops

    meta_a = build_mlops.load_assumptions()[1]
    checks = build_mlops.coupling_checks(
        meta_a,
        build_mlops._yaml("cost_model.yaml"),
        build_mlops._yaml("ragops_plan.yaml"),
        build_mlops._yaml("churn_handoff.yaml"),
        pd.read_csv(ROOT / "evidence" / "own_testing_claims.csv", dtype=str),
        build_mlops.load_matrix(),
        build_mlops.load_contracts(),
    )
    assert [c for c in checks if not c[1]] == []
