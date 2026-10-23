"""Threshold gates shared by the churn go-live check and the RAG release gate."""

from __future__ import annotations

import math
from collections.abc import Mapping


def release_gate(metrics: Mapping[str, float], thresholds: Mapping[str, Mapping[str, float]]) -> list[str]:
    """Return a failure message per threshold missed. A missing or NaN metric fails."""
    failures = []
    for name, bound in thresholds.items():
        value = metrics.get(name)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            failures.append(f"{name}: not measured")
            continue
        if "min" in bound and value < bound["min"]:
            failures.append(f"{name}: {value:.3g} < {bound['min']}")
        if "max" in bound and value > bound["max"]:
            failures.append(f"{name}: {value:.3g} > {bound['max']}")
    return failures


def signoff_status(rows: list[dict]) -> tuple[int, int]:
    signed = sum(str(r["decision"]).startswith("Approved") for r in rows)
    return signed, len(rows)
