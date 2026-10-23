"""Load contracts and the upstream artifacts they must conform to."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import Dataset, Register

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = ROOT / "contracts"
SCHEMAS_DIR = CONTRACTS_DIR / "schemas"
REGISTER_FILE = CONTRACTS_DIR / "consumers.yaml"
SECURITY_FILE = ROOT / "security" / "data_matrix.yaml"
ASSUMPTIONS_FILE = ROOT / "assumptions" / "assumptions.yaml"


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class ContractLayer:
    register: Register
    datasets: dict[str, Dataset]
    security: dict
    assumptions: dict[str, dict]

    def consumers_of(self, dataset: str):
        return [c for c in self.register.consumers if dataset in c.datasets]


def load_dataset(path: Path) -> Dataset:
    return Dataset.model_validate(_yaml(path))


def load(
    register_file: Path = REGISTER_FILE,
    schemas_dir: Path = SCHEMAS_DIR,
    security_file: Path = SECURITY_FILE,
    assumptions_file: Path = ASSUMPTIONS_FILE,
) -> ContractLayer:
    datasets = {ds.name: ds for ds in map(load_dataset, sorted(schemas_dir.glob("*.yaml")))}
    assumptions = {a["id"]: a for a in _yaml(assumptions_file)["assumptions"]}
    return ContractLayer(
        register=Register.model_validate(_yaml(register_file)),
        datasets=datasets,
        security=_yaml(security_file),
        assumptions=assumptions,
    )
