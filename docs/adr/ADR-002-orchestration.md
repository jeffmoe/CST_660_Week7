# ADR-002: Asset-based orchestration with dbt as the transformation layer

**Status:** Proposed (Accepted only after gates G1–G4 below are signed)
**Date:** 2026-10-23
**Owner:** Lead Analytics Engineer, Subscription Health data product
**Decision area:** Orchestration
**Depends on:** ADR-001
**Door type:** Two-way. Business logic stays in dbt SQL and plain Python modules, so switching orchestrators rewrites only the wiring (schedules, sensors, dependencies, checks).

> Citation key: `[E-xx-nn, label]` evidence row in `evidence/evidence_sources.csv`;
> `[A-xxx-nn]` assumption in `assumptions/assumptions.yaml`; `[C-xx-nn, own testing]`
> claim in `evidence/own_testing_claims.csv` (not yet run); `[projections]`
> `assumptions/derived/projections.csv`; `[judgement]` engineering judgement with no
> external evidence, called out so reviewers can challenge it.

## Context

- [ADR-001] puts storage and SQL transformation in one managed warehouse with dbt. The orchestrator therefore schedules dbt; it does not do heavy compute itself [ADR-001].
- The pipeline crosses systems: event loads [ADR-003] → dbt marts → churn feature publish for the ML team → transcript chunk-and-embed jobs in Python calling an external model API [ADR-004] [A-TRN-01].
- Retention marts and churn features must be fresh by 07:00 daily [A-OPS-02], and incremental models must reprocess a 3-day late-arriving window every run [A-OPS-03].
- Data-quality failures need to stop at the producer, not at the retention dashboard. Pipeline data issues have well-catalogued root causes that tests can catch [E-OR-02, peer-reviewed], and ML feature hand-offs need explicit data and feature tests [E-OR-03, peer-reviewed].
- Team is 3–5 people with no platform engineer [A-OPS-01]; orchestration may use ~8% of the $180k/yr ceiling, ~$14.4k/yr [A-BUD-01] [A-BUD-02].
- A 12-month backfill must fit an 8-hour overnight window [C-OR-01, own testing].

## Options considered

### Option A: Asset-based orchestrator (Dagster, managed) + dbt (chosen)

Each dbt model, feature table and embedding job is a declared data asset with an owner, freshness policy and asset checks. The orchestrator derives run order from asset lineage and loads the dbt project directly.

- **Genuine case for it:** asset-centric definitions with built-in lineage match "data product with named owners" better than task graphs [E-OR-04, vendor marketing] [judgement].
- **Genuine case for it:** asset checks can block downstream publish when tests fail, which is the control the pipeline-quality literature asks for [E-OR-02, peer-reviewed] [E-OR-03, peer-reviewed].
- **Genuine case for it:** one orchestrated path from raw to features avoids the glue-code and pipeline-jungle debt that hits ML systems [E-OR-01, peer-reviewed].
- **Case against:** the only evidence for asset orientation is the vendor's own writing [E-OR-04, vendor marketing].
- **Case against:** smaller ecosystem and hiring pool than Airflow [E-OR-05, vendor marketing] [E-OR-06, practitioner benchmark].

### Option B: Apache Airflow (managed service) + dbt

A managed Airflow deployment runs task DAGs that call dbt, loaders and embedding jobs. Lineage and data-quality gates come from dbt tests plus add-on tooling.

- **Genuine case for it:** the most widely adopted orchestrator, so hiring and ecosystem risk are lowest [E-OR-05, vendor marketing] [E-OR-06, practitioner benchmark].
- **Genuine case for it:** several managed offerings exist, reducing operational load for a team with no platform engineer [A-OPS-01] [judgement].
- **Genuine case for it:** dbt tests still gate data quality regardless of orchestrator, so quality coverage is not lost [E-OR-03, peer-reviewed].
- **Case against:** task-centric DAGs model "run this step," not "this table is fresh and valid"; asset ownership and freshness must be bolted on [E-OR-04, vendor marketing] [judgement].
- **Case against:** the adoption evidence comes from vendor and vendor-adjacent surveys, which measure popularity, not fitness for Aurora [E-OR-05, vendor marketing] [E-OR-06, practitioner benchmark].

This was the closest call. If the pilot shows Airflow meets [C-OR-01, own testing] and [C-OR-02, own testing] while Dagster does not, Airflow wins.

### Option C: Warehouse-native / dbt-scheduler only

Use the dbt platform's job scheduler and the warehouse's native tasks; no separate orchestrator.

- **Genuine case for it:** fewest components and lowest cost for a 3–5 person team; dbt tests still run [A-OPS-01] [E-OR-03, peer-reviewed].
- **Genuine case for it:** dbt-centric workflows are the norm for analytics-engineering teams of this profile [E-OR-06, practitioner benchmark].
- **Case against:** cannot express cross-system dependencies (loader completed → dbt → feature publish → Python embedding job) in one place, which recreates glue code and hidden coupling [E-OR-01, peer-reviewed].
- **Case against:** pushes scheduling into warehouse-proprietary features, which [ADR-001] explicitly avoids to keep its reversal cost low [ADR-001].

## Decision

- Adopt **Option A, an asset-based orchestrator (Dagster, managed)**, with **Option B as the named fallback**. The pilot in [C-OR-01, own testing], [C-OR-02, own testing] and [C-OR-03, own testing] runs both and can overturn this choice.
- Orchestration is wiring only. Business logic lives in dbt SQL and importable Python modules with their own tests, so either orchestrator can call them [judgement] [E-OR-01, peer-reviewed].
- Every published asset (mart, feature table, embedding batch) declares an owner, a freshness policy tied to the 07:00 SLA [A-OPS-02], and blocking checks. A failed check stops downstream publish [E-OR-02, peer-reviewed] [E-OR-03, peer-reviewed].
- Incremental models reprocess the late-arriving window from [A-OPS-03] on every run; backfills are a first-class, parameterized job, not ad-hoc scripts [C-OR-01, own testing].
- Production schedules, backfills and check thresholds change only through reviewed pull requests. [ADR-004] places these files in its "named human approval" zone [ADR-004].

## Strongest argument against

"You're picking the less popular tool on the strength of a vendor blog post. When your one data engineer leaves, the replacement will know Airflow." The adoption gap is real [E-OR-05, vendor marketing] [E-OR-06, practitioner benchmark]. The mitigation is that logic lives in dbt and plain Python, so the orchestrator layer stays thin and portable, and the onboarding test in [C-OR-03, own testing] measures the learning cost directly instead of assuming it [judgement].

## Consequences

**Easier**

- Lineage from raw events to churn features and embeddings is visible in one place, which supports incident triage and the ML team's audit needs [E-OR-01, peer-reviewed] [E-OR-03, peer-reviewed].
- Freshness against the 07:00 SLA is monitored per asset rather than inferred from task success [A-OPS-02] [judgement].

**Harder**

- One more managed service on the bill (~$14.4k/yr allocation) and one more thing on call for a 3–5 person team [A-BUD-02] [A-OPS-01].
- Check thresholds become production config; badly tuned checks either page constantly or miss faults, which [C-OR-02, own testing] measures [E-OR-02, peer-reviewed].

### Reversal cost

| Item | Estimate | Basis |
|:--|:--|:--|
| Rewrite asset/DAG definitions, schedules, sensors | 2–4 engineer-weeks | [judgement] [A-OPS-01] |
| Re-implement freshness policies and asset checks as dbt tests + alerts | 1 engineer-week | [judgement] [E-OR-03, peer-reviewed] |
| dbt models and Python modules | ~0 (unchanged by design) | [judgement] |
| Parallel run during cut-over | 2 weeks of both services, ~$1.2k | [A-BUD-02] |
| **Total** | **~3–5 engineer-weeks**, no data migration | [judgement] |

## Revisit triggers

- The pilot shows the fallback (Airflow) meets [C-OR-01, own testing] and [C-OR-02, own testing] while the chosen tool does not.
- Two consecutive hiring rounds fail to find candidates who can run the chosen orchestrator [E-OR-05, vendor marketing] [judgement].
- Orchestration spend exceeds 125% of its allocation for two consecutive months [C-BAS-02, own testing].

## Human-review gates

| Gate | What is reviewed | Reviewer (role) | Blocks |
|:--|:--|:--|:--|
| G1 Pilot result | Backfill time, seeded-fault detection and onboarding time from [C-OR-01, own testing], [C-OR-02, own testing], [C-OR-03, own testing] for both tools | Lead Analytics Engineer + one engineer outside the pilot | Tool selection |
| G2 On-call ownership | Who is paged for which assets; escalation path; runbook | Head of Data | Production schedules |
| G3 Check policy | Which checks block publish vs warn; thresholds for churn feature tables | ML Engineering lead | First feature publish |
| G4 Cost | Managed-service pricing vs [A-BUD-02] orchestration line | Finance (CFO delegate) | Contract signature |

## Evidence carried forward

| ID | Label | What it supports here | What it does not support |
|:--|:--|:--|:--|
| E-OR-01 | peer-reviewed | One orchestrated path beats glue code (argues against Option C) | A choice between A and B |
| E-OR-02 | peer-reviewed | Which data issues the checks must catch | Any tool's ability to catch them |
| E-OR-03 | peer-reviewed | Feature hand-off needs explicit tests and gates | Orchestrator choice |
| E-OR-04 | vendor marketing | The asset-orientation argument for Option A | That it works better in practice |
| E-OR-05 | vendor marketing | Airflow's adoption and hiring pool (Option B) | Fitness for Aurora |
| E-OR-06 | practitioner benchmark | Analytics-engineering teams are dbt-centric | Orchestrator performance |
| E-OR-07 | own testing | **Not yet run.** The only evidence that can separate A from B | — |

**Plainly:** the peer-reviewed evidence rules out Option C and says what any orchestrator must enforce, but nothing external separates A from B. Both cases rest on vendor marketing. The decision is provisional until own testing runs.

**Claims requiring own testing before acceptance:** [C-OR-01, own testing], [C-OR-02, own testing], [C-OR-03, own testing].

## Sign-off

The orchestration section of the spec may not be written until every row below is signed. ADR-001 must be signed first.

| Role | Name | Decision | Date |
|:--|:--|:--|:--|
| Lead Analytics Engineer (owner) | | Pending | |
| Head of Data | | Pending | |
| ML Engineering lead | | Pending | |
| Finance (CFO delegate) | | Pending | |
