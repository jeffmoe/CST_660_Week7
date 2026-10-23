"""End-to-end: generate synthetic data and validate the specs against it."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import generate_synthetic
import validate_synthetic
from conftest import SYNTHETIC_ARGS


@pytest.fixture(scope="module")
def generated(synthetic_data):
    return synthetic_data


def test_generation_is_deterministic(generated, tmp_path):
    again = tmp_path / "again"
    generate_synthetic.main([*SYNTHETIC_ARGS, "--out", str(again)])
    for name in ("features.churn_features", "rag.transcript_chunks"):
        pd.testing.assert_frame_equal(pd.read_parquet(generated / f"{name}.parquet"), pd.read_parquet(again / f"{name}.parquet"))


def test_manifest_lists_seeded_edge_cases(generated):
    ids = {c["id"] for c in json.loads((generated / "manifest.json").read_text())["edge_cases"]}
    assert {"EC-01", "EC-02", "EC-03", "EC-04", "EC-05", "EC-13"} <= ids


def test_specs_validate_against_generated_data(generated, tmp_path):
    v = validate_synthetic.Validator(generated, tmp_path / "report.md", tmp_path / "lineage.md")
    assert v.run() == 0, [c for c in v.checks if c.required and not c.passed]
    assert "Result: PASS" in (tmp_path / "report.md").read_text(encoding="utf-8")
