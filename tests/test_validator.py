"""Contract validator: exit codes, CLI behavior and the seeded-violation demonstration."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pandas as pd
import pytest

import demo_validator
import validate_contract
from aurora_contracts.governance import CheckContext
from aurora_contracts.validator import BLOCKED, FAIL, PASS, validate


def test_demo_matches_expected_output_for_every_seeded_violation(synthetic_data, tmp_path):
    demo = demo_validator.Demo(synthetic_data, tmp_path / "validator_demo.md")
    assert demo.run() == 0
    text = (tmp_path / "validator_demo.md").read_text(encoding="utf-8")
    assert "16/16 cases match" in text and "VERDICT: FAIL (BLOCKED)" in text


@pytest.mark.parametrize(("case", "code"), [("V-01", FAIL), ("V-05", BLOCKED), ("V-00c", PASS)])
def test_cli_exit_codes_on_seeded_batches(synthetic_data, tmp_path, case, code):
    demo = demo_validator.Demo(synthetic_data, tmp_path / "doc.md")
    spec = next(c for c in demo_validator.yaml.safe_load(demo_validator.CASES.read_text(encoding="utf-8"))["cases"] if c["id"] == case)
    assert demo.run_case(spec)["actual"]["exit_code"] == code


def test_unknown_dataset_is_rejected():
    buf = io.StringIO()
    args = validate_contract.parse_args(["no.such_dataset"])
    assert validate_contract.run(args, df=pd.DataFrame(), out=buf) == 2
    assert "Unknown dataset" in buf.getvalue()


def test_fail_on_must_be_known(layer):
    ds = layer.datasets["marts.retention_events_hourly"]
    with pytest.raises(ValueError):
        validate(pd.DataFrame(), ds, CheckContext(now=datetime.now(timezone.utc)), fail_on="sometimes")
