"""Contract conformance to ADR-001..003 / security matrix, and generated schema."""

from __future__ import annotations

import jsonschema
import pytest
from pydantic import ValidationError

from aurora_contracts import codegen, conformance
from aurora_contracts.loader import ContractLayer
from aurora_contracts.models import Dataset


def with_dataset(layer: ContractLayer, name: str, **update) -> ContractLayer:
    datasets = dict(layer.datasets)
    datasets[name] = datasets[name].model_copy(update=update)
    return ContractLayer(layer.register, datasets, layer.security, layer.assumptions)


def failed_checks(layer: ContractLayer) -> set[str]:
    return {f.check for f in conformance.check(layer) if not f.passed}


def test_shipped_contracts_conform(layer):
    assert failed_checks(layer) == set()


def test_register_covers_three_consumers(layer):
    assert {c.id for c in layer.register.consumers} == {"CON-RET", "CON-ML", "CON-RAG"}


def test_short_lookback_violates_late_arrival_window(layer):
    assert "C10" in failed_checks(with_dataset(layer, "features.churn_features", incremental_lookback_days=1))


def test_streaming_level_freshness_violates_adr_003(layer):
    ds = layer.datasets["marts.retention_events_hourly"]
    rules = [r.model_copy(update={"max_age_minutes": 5}) if r.rule == "freshness" else r for r in ds.governance]
    assert "C08" in failed_checks(with_dataset(layer, ds.name, governance=rules))


def test_restricted_column_in_retention_mart_exceeds_consumer_clearance(layer):
    ds = layer.datasets["marts.retention_subscription_health_daily"]
    email = ds.columns[0].model_copy(
        update={"name": "email", "pii": True, "pii_element": "PII-SUB-01", "classification": "C3", "nullable": True}
    )
    assert "C22" in failed_checks(with_dataset(layer, ds.name, columns=[*ds.columns, email]))


def test_feature_table_without_open_format_export_violates_adr_001(layer):
    assert "C03" in failed_checks(with_dataset(layer, "features.churn_features", open_format_export=False))


def test_pii_column_requires_inventory_element():
    with pytest.raises(ValidationError, match="pii_element"):
        Dataset.model_validate(
            {
                "name": "x.y", "version": 1, "description": "d", "owner": "o", "layer": "marts",
                "platform": "warehouse", "produced_by": "dbt:y", "grain": "g", "primary_key": ["a"],
                "materialization": "table", "schedule": "daily", "retention_rule": "R-01", "governance": [],
                "columns": [{"name": "a", "type": "string", "description": "d", "classification": "C3", "pii": True}],
            }
        )


# ----------------------------------------------------------------- codegen
@pytest.fixture
def event_schema(layer):
    return codegen.event_json_schema(layer.datasets["raw.subscription_events"])


GOOD_EVENT = {
    "event_id": "6f1c0e2a-1f5e-4d2b-9a57-0d9a3c1b2e44",
    "event_type": "subscription_cancelled",
    "source": "subscription_platform",
    "event_time": "2027-03-09T14:03:00Z",
    "subscriber_id": "sub_00042",
    "anonymous_id": None,
    "ip_address": None,
    "user_agent": None,
    "properties": {"reason": "too_expensive"},
}


def test_event_schema_accepts_valid_event(event_schema):
    jsonschema.validate(GOOD_EVENT, event_schema)


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in GOOD_EVENT.items() if k != "event_id"},
        {**GOOD_EVENT, "event_type": "subscription_upgraded"},
        {**GOOD_EVENT, "ingested_at": "2027-03-09T14:05:00Z"},
    ],
    ids=["missing-event-id", "unknown-event-type", "producer-sets-ingested-at"],
)
def test_event_schema_rejects_invalid_event(event_schema, bad):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, event_schema)


@pytest.mark.parametrize("dialect", list(codegen.DIALECTS))
def test_dbt_models_enforce_contracts(layer, dialect):
    schema = codegen.dbt_schema(layer, dialect)
    assert {m["name"] for m in schema["models"]} == {
        "retention_subscription_health_daily", "retention_events_hourly", "churn_features_v1", "rag_transcript_chunks"
    }
    assert all(m["config"]["contract"]["enforced"] for m in schema["models"])
    assert schema["sources"][0]["tables"][0]["loaded_at_field"] == "ingested_at"
