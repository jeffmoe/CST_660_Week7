# Architecture Decision Records — Subscription Health data product

| ADR | Decision | Depends on | Status | Own testing still open |
|:--|:--|:--|:--|:--|
| [ADR-001](ADR-001-warehouse-vs-lakehouse.md) | Warehouse-centric storage (lakehouse and hybrid evaluated) | — | Proposed | C-WL-01, C-WL-02 |
| [ADR-002](ADR-002-orchestration.md) | Asset-based orchestrator + dbt (Airflow is named fallback) | ADR-001 | Proposed | C-OR-01, C-OR-02, C-OR-03 |
| [ADR-003](ADR-003-streaming-vs-micro-batch.md) | Micro-batch ingestion, 15-min landing (streaming and daily batch evaluated) | ADR-001, ADR-002 | Proposed | C-ST-01, C-ST-02, C-BAS-03 |
| [ADR-004](ADR-004-agentic-ai-permitted-zones.md) | Bounded agentic zones with human-review gates | ADR-001, ADR-002, ADR-003 | Proposed | C-AI-01 … C-AI-05 |

## Rules every ADR follows

1. **Context, options, decision, consequences, human-review gates, cited evidence, sign-off.**
2. **At least one alternative is genuinely evaluated.** Every rejected option states its
   strongest case, backed by an evidence row or an assumption, not only judgement.
3. **Consequences include a reversal-cost table.**
4. **Every substantive claim is cited by evidence label:**
   - `[E-xx-nn, label]`: row in `evidence/evidence_sources.csv`; the label must match
     (`vendor marketing`, `peer-reviewed`, `practitioner benchmark`, `own testing`)
   - `[A-xxx-nn]`: assumption in `assumptions/assumptions.yaml`
   - `[C-xx-nn, own testing]`: claim in `evidence/own_testing_claims.csv`, not yet run
   - `[projections]`: `assumptions/derived/projections.csv`
   - `[judgement]`: engineering judgement with no external evidence, labeled so
     reviewers can challenge it
5. **The evidence table is carried forward:** every source for the ADR's decision area
   appears with what it does and does not support.

`python scripts/check_adrs.py` enforces rules 1–5 and fails on any violation.

## Order and human-review gate

ADRs are decided in order. ADR-004 is written and signed **last**, because its
permitted zones depend on where data lives (ADR-001), what owns schedules and checks
(ADR-002), and how fresh and late the data is (ADR-003).

**No section of the spec may be written for an ADR until every row of that ADR's
sign-off table is approved, and its dependencies are signed first.** The checker reports
each ADR's spec gate as `OPEN` or `BLOCKED (n/m signed)` and fails if an ADR is fully
signed before its dependencies.

Each ADR is `Proposed` until its own-testing claims have run and met their thresholds;
sign-off on a Proposed ADR approves the direction and the test plan, not the result.
