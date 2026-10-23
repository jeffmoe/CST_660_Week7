"""Typed models for the contract layer.

YAML under contracts/ is parsed into these pydantic models, so a malformed
contract fails at load time with a precise error rather than at run time.
"""

from __future__ import annotations

from datetime import time
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

CLASSIFICATION_ORDER = ("C0", "C1", "C2", "C3", "C4")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ schema
class DataType(str, Enum):
    """Warehouse-neutral logical types; mapped to the dialect chosen in ADR-001."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    JSON = "json"
    ARRAY_STRING = "array<string>"


class Column(Strict):
    name: str
    type: DataType
    nullable: bool = True
    description: str
    pii: bool = False
    classification: Literal["C0", "C1", "C2", "C3", "C4"]
    pii_element: str | None = None  # PII-xxx-nn in security/data_matrix.yaml
    health_tagged: bool = False
    unique: bool = False
    accepted_values: list[str] | None = None
    min_value: float | None = None
    max_value: float | None = None
    stable: bool = False  # stable-contract field: breaking change needs ML lead (ADR-004 Zone 2)

    @model_validator(mode="after")
    def _pii_needs_element(self) -> "Column":
        if self.pii and not self.pii_element:
            raise ValueError(f"column {self.name}: pii=true requires pii_element")
        return self


# ------------------------------------------------------------------- rules
class FailureBehavior(str, Enum):
    """What happens when a governance rule fails. Mapped to orchestrator asset checks (ADR-002)."""

    BLOCK_PUBLISH = "block_publish"
    QUARANTINE_ROWS = "quarantine_rows"
    SERVE_STALE_AND_ALERT = "serve_stale_and_alert"
    SECURITY_INCIDENT = "security_incident"
    WARN = "warn"

    @property
    def blocking(self) -> bool:
        return self in {FailureBehavior.BLOCK_PUBLISH, FailureBehavior.SECURITY_INCIDENT}


class _Rule(Strict):
    id: str
    on_fail: FailureBehavior
    description: str


class SchemaRule(_Rule):
    rule: Literal["schema"]


class NotNullRule(_Rule):
    rule: Literal["not_null"]  # columns come from nullable=false in the schema


class UniqueRule(_Rule):
    rule: Literal["unique"]
    columns: list[str]


class PiiScanRule(_Rule):
    rule: Literal["pii_scan"]
    columns: list[str]
    patterns: list[Literal["email", "us_phone", "card_number", "us_ssn"]]
    # Patterns whose hit is Prohibited (C4) data escalate to a security incident.
    incident_patterns: list[Literal["card_number", "us_ssn"]] = []


class FreshnessRule(_Rule):
    rule: Literal["freshness"]
    timestamp_column: str
    max_age_minutes: int | None = None
    deadline_local: time | None = None  # daily partitions must exist by this time
    partition_column: str | None = None
    escalate_after_consecutive: int = 2
    escalate_to: FailureBehavior = FailureBehavior.BLOCK_PUBLISH

    @model_validator(mode="after")
    def _one_mode(self) -> "FreshnessRule":
        if (self.max_age_minutes is None) == (self.deadline_local is None):
            raise ValueError(f"{self.id}: set exactly one of max_age_minutes or deadline_local")
        if self.deadline_local and not self.partition_column:
            raise ValueError(f"{self.id}: deadline mode needs partition_column")
        return self


class ConsentScopeRule(_Rule):
    rule: Literal["consent_scope"]
    purpose: str  # P-id in security/data_matrix.yaml
    subject_column: str = "subscriber_id"
    require: dict[str, bool]  # consent attribute -> required value
    required_gates: list[str] = []  # review gates that must be cleared (e.g. SEC-G3)
    max_quarantine_share: float = 0.01
    escalate_to: FailureBehavior = FailureBehavior.BLOCK_PUBLISH


Rule = Annotated[
    Union[SchemaRule, NotNullRule, UniqueRule, PiiScanRule, FreshnessRule, ConsentScopeRule],
    Field(discriminator="rule"),
]


# ----------------------------------------------------------------- dataset
class Dataset(Strict):
    name: str
    version: int
    description: str
    owner: str
    layer: Literal["raw", "marts", "features", "rag"]
    platform: Literal["warehouse"]  # ADR-001: one managed warehouse
    produced_by: str  # "dbt:<model>" or "collector:<name>"
    grain: str
    primary_key: list[str]
    materialization: Literal["append", "incremental", "table", "snapshot"]
    incremental_lookback_days: int | None = None
    schedule: str
    retention_rule: str  # R-nn in security/data_matrix.yaml
    open_format_export: bool = False
    columns: list[Column]
    governance: list[Rule]

    @property
    def classification(self) -> str:
        return max((c.classification for c in self.columns), key=CLASSIFICATION_ORDER.index)

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)

    @model_validator(mode="after")
    def _keys_and_rules_reference_columns(self) -> "Dataset":
        names = {c.name for c in self.columns}
        if len(names) != len(self.columns):
            raise ValueError(f"{self.name}: duplicate column names")
        for k in self.primary_key:
            if k not in names:
                raise ValueError(f"{self.name}: primary key {k} is not a column")
        for r in self.governance:
            cols = getattr(r, "columns", []) + [
                getattr(r, a) for a in ("timestamp_column", "partition_column", "subject_column") if getattr(r, a, None)
            ]
            for c in cols:
                if c not in names:
                    raise ValueError(f"{self.name}: rule {r.id} references unknown column {c}")
        return self


# ---------------------------------------------------------------- consumer
class Sla(Strict):
    daily_deadline_local: time | None = None
    max_lag_minutes: int | None = None
    availability: str


class Consumer(Strict):
    id: str
    name: str
    owner: str
    description: str
    datasets: list[str]
    roles: list[str]  # rbac_roles in security/data_matrix.yaml
    purposes: list[str]  # consent purposes
    max_classification: Literal["C0", "C1", "C2", "C3"]
    required_gates: list[str] = []
    sla: Sla
    on_breach: str
    guarantees: list[str]
    change_policy: str
    breaking_change_approvers: list[str]


class Register(Strict):
    version: str
    last_reviewed: str
    consumers: list[Consumer]
