"""Generate the physical schema from contracts: dbt model contracts and the
event JSON Schema. These outputs feed the spec's schema section.

ADR-001 picks the warehouse vendor by PoC, so dbt files are generated per
candidate dialect; delete the loser after vendor selection.
"""

from __future__ import annotations

from .conformance import MICRO_BATCH_LANDING_MINUTES
from .loader import ContractLayer
from .models import Dataset, DataType, FreshnessRule, UniqueRule

DIALECTS: dict[str, dict[DataType, str]] = {
    "snowflake": {
        DataType.STRING: "varchar",
        DataType.INTEGER: "number(38,0)",
        DataType.NUMBER: "float",
        DataType.BOOLEAN: "boolean",
        DataType.DATE: "date",
        DataType.TIMESTAMP: "timestamp_tz",
        DataType.JSON: "variant",
        DataType.ARRAY_STRING: "array",
    },
    "bigquery": {
        DataType.STRING: "string",
        DataType.INTEGER: "int64",
        DataType.NUMBER: "float64",
        DataType.BOOLEAN: "bool",
        DataType.DATE: "date",
        DataType.TIMESTAMP: "timestamp",
        DataType.JSON: "json",
        DataType.ARRAY_STRING: "array<string>",
    },
}

_JSON_TYPES = {
    DataType.STRING: {"type": "string"},
    DataType.INTEGER: {"type": "integer"},
    DataType.NUMBER: {"type": "number"},
    DataType.BOOLEAN: {"type": "boolean"},
    DataType.DATE: {"type": "string", "format": "date"},
    DataType.TIMESTAMP: {"type": "string", "format": "date-time"},
    DataType.JSON: {"type": "object"},
    DataType.ARRAY_STRING: {"type": "array", "items": {"type": "string"}},
}


def _column_tests(col, single_unique: bool, as_source: bool) -> list:
    tests: list = []
    if as_source and not col.nullable:
        tests.append("not_null")
    if single_unique:
        tests.append("unique")
    if col.accepted_values:
        tests.append({"accepted_values": {"values": col.accepted_values}})
    if col.min_value is not None or col.max_value is not None:
        rng = {k: v for k, v in (("min_value", col.min_value), ("max_value", col.max_value)) if v is not None}
        tests.append({"dbt_utils.accepted_range": rng})
    return tests


def _columns(ds: Dataset, dialect: str, as_source: bool = False) -> list[dict]:
    # dbt enforces constraints on model contracts only; sources get equivalent tests.
    uniques = {tuple(r.columns) for r in ds.governance if isinstance(r, UniqueRule)}
    out = []
    for c in ds.columns:
        entry: dict = {
            "name": c.name,
            "data_type": DIALECTS[dialect][c.type],
            "description": c.description,
            "meta": {
                "classification": c.classification,
                "pii": c.pii,
                **({"pii_element": c.pii_element} if c.pii_element else {}),
                **({"stable_contract_field": True} if c.stable else {}),
            },
        }
        if not c.nullable and not as_source:
            entry["constraints"] = [{"type": "not_null"}]
        tests = _column_tests(c, c.unique or (c.name,) in uniques, as_source)
        if tests:
            entry["data_tests"] = tests
        out.append(entry)
    return out


def _governance_meta(ds: Dataset) -> list[dict]:
    return [
        {
            "id": r.id,
            "rule": r.rule,
            "on_fail": r.on_fail.value,
            **({"escalate_to": r.escalate_to.value} if hasattr(r, "escalate_to") else {}),
        }
        for r in ds.governance
    ]


def dbt_schema(layer: ContractLayer, dialect: str) -> dict:
    models, sources = [], []
    for ds in layer.datasets.values():
        meta = {
            "owner": ds.owner,
            "contract_version": ds.version,
            "classification": ds.classification,
            "retention_rule": ds.retention_rule,
            "consumers": [c.id for c in layer.consumers_of(ds.name)],
            "governance": _governance_meta(ds),
        }
        if ds.produced_by.startswith("collector:"):
            schema, table = ds.name.split(".")
            fresh = next(r for r in ds.governance if isinstance(r, FreshnessRule))
            sources.append(
                {
                    "name": schema,
                    "tables": [
                        {
                            "name": table,
                            "description": ds.description,
                            "loaded_at_field": fresh.timestamp_column,
                            "freshness": {
                                "warn_after": {"count": fresh.max_age_minutes, "period": "minute"},
                                # Each consecutive miss is one 15-minute landing cycle (ADR-003).
                                "error_after": {
                                    "count": fresh.escalate_after_consecutive * MICRO_BATCH_LANDING_MINUTES,
                                    "period": "minute",
                                },
                            },
                            "meta": meta,
                            "columns": _columns(ds, dialect, as_source=True),
                        }
                    ],
                }
            )
            continue
        composite = [r.columns for r in ds.governance if isinstance(r, UniqueRule) and len(r.columns) > 1]
        config = {"contract": {"enforced": True}, "materialized": ds.materialization, "meta": meta}
        if ds.incremental_lookback_days:
            config["meta"]["incremental_lookback_days"] = ds.incremental_lookback_days
        model = {
            "name": ds.produced_by.removeprefix("dbt:"),
            "description": ds.description,
            "config": config,
            "columns": _columns(ds, dialect),
        }
        if composite:
            model["data_tests"] = [
                {"dbt_utils.unique_combination_of_columns": {"combination_of_columns": cols}} for cols in composite
            ]
        models.append(model)
    return {"version": 2, "sources": sources, "models": models}


def event_json_schema(ds: Dataset) -> dict:
    props = {}
    for c in ds.columns:
        spec = dict(_JSON_TYPES[c.type])
        if c.accepted_values:
            spec["enum"] = c.accepted_values
        if c.nullable:
            spec = {"anyOf": [spec, {"type": "null"}]}
        props[c.name] = {**spec, "description": c.description}
    # ingested_at is stamped by the loader, not by producers.
    required = [c.name for c in ds.columns if not c.nullable and c.name != "ingested_at"]
    producer_props = {k: v for k, v in props.items() if k != "ingested_at"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://aurora-nutrition.example/contracts/{ds.name}/v{ds.version}.json",
        "title": f"{ds.name} v{ds.version} (producer payload)",
        "description": ds.description,
        "type": "object",
        "properties": producer_props,
        "required": required,
        "additionalProperties": False,
    }
