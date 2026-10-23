"""Evaluation metrics for the churn model and the RAG retriever."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from sklearn.metrics import brier_score_loss, roc_auc_score


# --------------------------------------------------------------- churn model
def precision_at_top_fraction(y_true: Sequence[int], scores: Sequence[float], fraction: float = 0.1) -> float:
    """Churn rate among the highest-scored `fraction` of subscribers (the outreach list)."""
    y, s = np.asarray(y_true), np.asarray(scores)
    k = max(1, int(round(len(s) * fraction)))
    top = np.argsort(-s, kind="stable")[:k]
    return float(y[top].mean())


def lift_at_top_fraction(y_true: Sequence[int], scores: Sequence[float], fraction: float = 0.1) -> float:
    base = float(np.mean(y_true))
    return precision_at_top_fraction(y_true, scores, fraction) / base if base else float("nan")


def expected_calibration_error(y_true: Sequence[int], probs: Sequence[float], bins: int = 10) -> float:
    y, p = np.asarray(y_true, dtype=float), np.asarray(probs, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            ece += mask.mean() * abs(p[mask].mean() - y[mask].mean())
    return float(ece)


def churn_model_kpis(y_true: Sequence[int], probs: Sequence[float]) -> dict[str, float]:
    return {
        "roc_auc": float(roc_auc_score(y_true, probs)),
        "precision_top_decile": precision_at_top_fraction(y_true, probs, 0.1),
        "lift_top_decile": lift_at_top_fraction(y_true, probs, 0.1),
        "brier": float(brier_score_loss(y_true, probs)),
        "ece": expected_calibration_error(y_true, probs),
    }


# ------------------------------------------------------------------ retrieval
def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(retrieved[:k]) & relevant) / len(relevant) if relevant else float("nan")


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str], k: int = 10) -> float:
    for rank, doc in enumerate(retrieved[:k], start=1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: set[str], k: int = 10) -> float:
    """Binary-relevance nDCG; the ideal ranking counts relevant items that were not retrieved."""
    if not relevant:
        return float("nan")
    discounts = 1.0 / np.log2(np.arange(2, k + 2))
    gains = np.array([1.0 if d in relevant else 0.0 for d in retrieved[:k]])
    dcg = float((gains * discounts[: len(gains)]).sum())
    idcg = float(discounts[: min(len(relevant), k)].sum())
    return dcg / idcg


def retrieval_report(runs: Sequence[tuple[Sequence[str], set[str]]], k: int = 5) -> dict[str, float]:
    """Mean retrieval metrics over (retrieved_ids, relevant_ids) pairs from the golden set."""
    return {
        f"recall@{k}": float(np.mean([recall_at_k(r, g, k) for r, g in runs])),
        "mrr@10": float(np.mean([reciprocal_rank(r, g, 10) for r, g in runs])),
        "ndcg@10": float(np.mean([ndcg_at_k(r, g, 10) for r, g in runs])),
        "hit_rate@1": float(np.mean([recall_at_k(r, g, 1) > 0 for r, g in runs])),
    }


def pass_hat_k(trial_outcomes: Sequence[Sequence[bool]], k: int) -> float:
    """pass^k: share of tasks that succeed in all of k repeated trials (reliability, E-AI-07)."""
    tasks = [t[:k] for t in trial_outcomes if len(t) >= k]
    return float(np.mean([all(t) for t in tasks])) if tasks else float("nan")
