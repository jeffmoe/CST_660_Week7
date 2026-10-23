"""Drift detection for churn features/scores and the RAG corpus and queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.spatial.distance import jensenshannon
from scipy.stats import ks_2samp


@dataclass(frozen=True)
class DriftResult:
    signal: str
    value: float
    status: str  # "ok" | "investigate" | "act"
    detail: str


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float:
    """Population stability index with quantile bins from the reference sample."""
    ref, cur = np.asarray(expected, dtype=float), np.asarray(actual, dtype=float)
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    eps = 1e-6
    p = np.histogram(ref, edges)[0] / len(ref) + eps
    q = np.histogram(cur, edges)[0] / len(cur) + eps
    return float(np.sum((q - p) * np.log(q / p)))


def psi_status(value: float, investigate: float = 0.1, act: float = 0.25) -> str:
    return "act" if value >= act else "investigate" if value >= investigate else "ok"


def feature_drift(reference: Mapping[str, Sequence[float]], current: Mapping[str, Sequence[float]]) -> list[DriftResult]:
    out = []
    for name, ref in reference.items():
        v = psi(ref, current[name])
        out.append(DriftResult(f"psi:{name}", v, psi_status(v), "PSI vs training reference"))
    return out


def similarity_shift(reference: Sequence[float], current: Sequence[float], p_threshold: float = 0.01,
                     median_shift: float = 0.05) -> DriftResult:
    """Top-1 retrieval similarity: KS test plus a practical-size guard on the median."""
    stat, p = ks_2samp(reference, current)
    shift = float(np.median(reference) - np.median(current))
    status = "act" if p < p_threshold and shift > median_shift else "investigate" if p < p_threshold else "ok"
    return DriftResult("top1_similarity", float(stat), status, f"KS p={p:.3g}, median drop {shift:.3f}")


def topic_divergence(reference: Mapping[str, float], current: Mapping[str, float], act: float = 0.1) -> DriftResult:
    """Jensen-Shannon distance between topic mixes (queries or new chunks)."""
    topics = sorted(set(reference) | set(current))
    p = np.array([reference.get(t, 0.0) for t in topics], dtype=float)
    q = np.array([current.get(t, 0.0) for t in topics], dtype=float)
    d = float(jensenshannon(p / p.sum(), q / q.sum(), base=2))
    return DriftResult("topic_js", d, "act" if d >= act else "investigate" if d >= act / 2 else "ok", f"topics: {topics}")


def centroid_shift(reference: np.ndarray, current: np.ndarray, act: float = 0.1) -> DriftResult:
    """Cosine distance between mean query embeddings of the golden set and this week's queries."""
    a, b = reference.mean(axis=0), current.mean(axis=0)
    d = float(1 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return DriftResult("query_centroid_cosine", d, "act" if d >= act else "investigate" if d >= act / 2 else "ok",
                       "1 - cos(mean golden-set query, mean current query)")


def low_similarity_rate(top1_scores: Sequence[float], floor: float, act: float = 0.15) -> DriftResult:
    rate = float(np.mean(np.asarray(top1_scores) < floor))
    return DriftResult("no_good_match_rate", rate, "act" if rate >= act else "ok",
                       f"share of queries with top-1 similarity < {floor}")
