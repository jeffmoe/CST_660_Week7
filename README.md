# CST_660_Week7
The Aurora Nutrition Subscription Health Data Product — ADR and Operating Plan

## Planning inputs (branch `assumptions`)

| Path | What it is |
|:--|:--|
| `assumptions/assumptions.yaml` | Assumptions register source: subscriber growth, event volume, transcript volume, storage, budget (low/base/high) |
| `evidence/evidence_sources.csv` | Evidence table: sources per decision area, labeled vendor marketing / peer-reviewed / practitioner benchmark / own testing |
| `evidence/own_testing_claims.csv` | Claims that cannot be sourced externally, with test method and pass threshold |
| `docs/assumptions_register.md`, `docs/evidence_table.md` | Generated reports (do not edit by hand) |
| `docs/adr/` | ADR-001 to ADR-004 with options, consequences, reversal cost, review gates and sign-off ([index](docs/adr/README.md)) |
| `security/data_matrix.yaml` → `docs/security_matrix.md` | Security & privacy matrix: classification, PII inventory, consent, retention (resolved from regulatory-scope assumptions), RBAC/ABAC, regulatory mapping, cross-border and re-identification review gates |
| `contracts/consumers.yaml`, `contracts/schemas/*.yaml` → `docs/contract_layer.md` | Contract layer: consumer register (retention, ML, RAG) and their contracts, schema definitions, governance rules with failure behaviors |
| `aurora_contracts/` | Governance rules as code (schema, null, unique, PII scan, freshness, consent scope), conformance checks, schema generators |
| `contracts/generated/` | Generated dbt model contracts (per ADR-001 candidate dialect) and the producer event JSON Schema |
| `mlops/*.yaml` → `docs/mlops_churn_handoff.md`, `docs/ragops_plan.md`, `docs/cost_operating_plan.md` | Churn-model handoff checklist + KPIs/SLAs; RAGOps plan (chunking, refresh, evaluation, drift, retrieval checks); cost-aware operating plan with sign-off gates COST-G1 and RAG-G1 |
| `aurora_mlops/` | Chunker, retrieval/churn metrics, drift detection, release gates, cost model |
| `assumptions/derived/projections.csv` | Generated month-by-month projections per scenario |

Regenerate after editing any source file:

```bash
pip install -r requirements.txt
python scripts/build_assumptions.py
python scripts/build_evidence.py   # also validates labels and own-testing coverage
python scripts/check_adrs.py       # validates ADR citations, options, evidence carry-forward, sign-off gate
python scripts/build_security_matrix.py  # resolves regimes and retention per scenario; validates coupling
python scripts/build_contracts.py  # validates contracts against ADR-001..003 + security matrix; generates schema
python scripts/build_mlops.py      # handoff, RAGOps and cost plans; checks coupling to ADR-003, contracts, assumptions
python -m pytest                  # governance-rule and contract tests
```
