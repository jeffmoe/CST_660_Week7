# ADR-001: Warehouse-centric storage for the Subscription Health data product

**Status:** Proposed (Accepted only after gates G1–G4 below are signed)
**Date:** 2026-10-23
**Owner:** Lead Analytics Engineer, Subscription Health data product
**Decision area:** Warehouse vs lakehouse
**Depends on:** none
**Door type:** Two-way for the first ~6 months, getting heavier with every model and consumer added. Data volume is small enough to copy; the cost of reversal is in rewriting SQL, access control and integrations.

> Citation key: `[E-xx-nn, label]` evidence row in `evidence/evidence_sources.csv`;
> `[A-xxx-nn]` assumption in `assumptions/assumptions.yaml`; `[C-xx-nn, own testing]`
> claim in `evidence/own_testing_claims.csv` (not yet run); `[projections]`
> `assumptions/derived/projections.csv`; `[judgement]` engineering judgement with no
> external evidence, called out so reviewers can challenge it.

## Context

- One product serves three consumers with different access patterns: retention dashboards (many small, repetitive queries every morning), the ML team's nightly churn-risk features, and the transcript corpus behind the retention assistant [A-OPS-02] [ADR-004].
- Data stays small through the fiscal year. Total stored footprint at month 12 is ~125 GB in the base scenario and ~900 GB in the high scenario we size against [projections] [A-GRW-02] [A-STO-01] [A-STO-03].
- The vector index is a rounding error by comparison: 2–20 GB at month 12 across base and high scenarios [projections] [A-TRN-05] [A-STO-04].
- The team is 3–5 people with no dedicated platform engineer, while subscribers grow ~6x [A-OPS-01] [A-GRW-02].
- Storage plus compute may use ~55% of a $180k/yr ceiling: $90k/yr compute (~$7.5k/month) and $9k/yr storage [A-BUD-01] [A-BUD-02].
- The ML team needs a 12-month feature set without manual export copies [C-WL-02, own testing].

## Options considered

### Option A: Warehouse-centric (managed cloud warehouse + dbt) (chosen)

Raw events, transcripts text, modeled marts and churn feature tables all live in one managed cloud warehouse. dbt holds transformation logic in SQL. The ML team reads features through the platform's native Python connector or an open-table-format export. Shortlist: two managed warehouses, final pick by PoC [C-WL-01, own testing].

- **Genuine case for it:** separated storage and elastic compute let small BI workloads scale down to near-zero cost overnight and up for the 07:00 rush [E-WL-01, peer-reviewed] [A-OPS-02].
- **Genuine case for it:** Aurora's dashboards are the many-small-repetitive-queries pattern that real cloud traces show, not the large scans TPC-DS measures [E-WL-05, peer-reviewed].
- **Genuine case for it:** default, untuned setups perform competitively in third-party comparison, which matches a 3–5 person team that will not tune heavily [E-WL-08, practitioner benchmark] [A-OPS-01].
- **Case against:** proprietary storage format and SQL dialect create lock-in, and ML reads go through a connector rather than direct file access [E-WL-02, peer-reviewed] [E-WL-03, peer-reviewed].
- **Case against:** per-second compute billing can surprise a small team without guardrails [judgement] [A-BUD-02].

### Option B: Lakehouse (object storage + open table format + Spark/SQL engine)

Data lands in object storage as Delta or Iceberg tables. One engine family serves dashboards via a SQL endpoint and the ML team via direct table reads.

- **Genuine case for it:** one copy of data serves BI and ML, removing the export step between the warehouse and model training [E-WL-02, peer-reviewed].
- **Genuine case for it:** ACID tables on object storage give the ML team direct, versioned reads of feature tables [E-WL-03, peer-reviewed] [C-WL-02, own testing].
- **Genuine case for it:** open formats (Delta, Iceberg, Hudi) lower vendor lock-in and so lower reversal cost [E-WL-04, peer-reviewed].
- **Genuine case for it:** the vendor reports a price/performance lead on official TPC-DS results [E-WL-06, vendor marketing], though the competing vendor disputes the comparison [E-WL-07, vendor marketing].
- **Case against:** the headline benchmark is at 100 TB; Aurora's high scenario is under 1 TB, so the scale advantage does not apply [E-WL-06, vendor marketing] [projections].
- **Case against:** open table formats add file-level maintenance (compaction, snapshot expiry, file sizing) and format choice that a team with no platform engineer must own [E-WL-04, peer-reviewed] [A-OPS-01].
- **Case against:** the ML workload is tabular churn features, not large-scale training on raw unstructured data, which is where the single-copy argument is strongest [E-WL-02, peer-reviewed] [judgement].

### Option C: Hybrid (warehouse for BI, separate lake for ML)

Dashboards run on a warehouse; a separate lake holds raw events and features for the ML team.

- **Genuine case for it:** each consumer gets the engine shaped for it: warehouse concurrency for dashboards, direct file reads for ML [E-WL-01, peer-reviewed] [E-WL-03, peer-reviewed].
- **Case against:** two copies of subscription state with a sync job between them is the "pipeline jungle" pattern that creates ML technical debt and training/serving skew [E-OR-01, peer-reviewed].
- **Case against:** two platforms to operate and pay for roughly doubles fixed overhead against a $90k/yr compute line [A-BUD-02] [A-OPS-01] [judgement].

## Decision

- Adopt **Option A, warehouse-centric**. The specific vendor is chosen from a two-platform shortlist by the PoC in [C-WL-01, own testing] and the feature-extract test in [C-WL-02, own testing].
- Size and benchmark against the **high** scenario (~900 GB at month 12) and budget against **base** [projections] [A-GRW-02].
- To keep the door two-way, three constraints apply [judgement] [E-WL-04, peer-reviewed]:
  1. All transformation logic lives in dbt SQL models; no business logic in proprietary stored procedures or vendor-only task schedulers (orchestration is [ADR-002]).
  2. The churn feature tables are published in an open table format if the chosen platform supports it, verified in [C-WL-02, own testing]; otherwise as a nightly Parquet snapshot.
  3. Every table carries a `pii` tag at creation, which [ADR-004] uses to bound agent access.
- Transcript text lives in the warehouse; the vector index is a separate component sized in the spec. At 2–20 GB it does not affect this decision [projections] [ADR-004].

## Strongest argument against

"You're optimizing for this year's data size and locking in a proprietary format right as the ML team starts. If the churn model moves to clickstream sequences or transcript embeddings as features, you'll want the lakehouse anyway, and you'll pay the migration then with more models built on top." This is a real risk [E-WL-02, peer-reviewed]. The response is the open-format constraint on feature tables [E-WL-04, peer-reviewed] and an explicit revisit trigger on ML workload shape below, not a claim that it cannot happen [judgement].

## Consequences

**Easier**

- One SQL surface for analytics engineers, dashboards and feature tables; dbt tests run where the data lives [E-OR-03, peer-reviewed].
- Compute scales to zero overnight, which matches a single 07:00 SLA [E-WL-01, peer-reviewed] [A-OPS-02].
- Warehouse roles and `pii` tags give [ADR-004] a single place to enforce agent access boundaries [judgement].

**Harder**

- The ML team depends on a connector or open-format export rather than direct file access; if [C-WL-02, own testing] fails, a nightly export job becomes permanent glue [E-OR-01, peer-reviewed].
- Compute cost must be actively governed (auto-suspend, resource monitors, query tagging) to stay within ~$7.5k/month [A-BUD-02] [C-BAS-02, own testing].

### Reversal cost

| Item | Estimate | Basis |
|:--|:--|:--|
| Copy data out (≤ 900 GB at month 12) | Hours, low egress cost | [projections] [A-STO-01] |
| Port dbt models to another SQL dialect | 3–6 engineer-weeks at ~60–100 models | [judgement] [A-OPS-01] |
| Rebuild roles, `pii` tags, BI connections, ML connector | 2–3 engineer-weeks | [judgement] |
| Dual-run both platforms for one month | ~1 month of compute line, ~$7.5k | [A-BUD-02] |
| **Total** | **~6–10 engineer-weeks + ~$7.5k**, ~2 months for a 4-person team | [judgement] [A-OPS-01] |

Reversal is cheapest before month 6, when ~240 GB and fewer models exist in the high scenario [projections]. The open-format feature tables mean the ML side of a reversal is mostly re-pointing reads [E-WL-04, peer-reviewed].

## Revisit triggers

- Stored footprint exceeds 5 TB (over 5x the high scenario) [projections] [A-STO-03].
- The ML team commits to training on raw event sequences or transcript embeddings as features [E-WL-02, peer-reviewed].
- [C-WL-01, own testing] fails on both shortlisted warehouses, or compute exceeds 125% of its allocation for two consecutive months [C-BAS-02, own testing].

## Human-review gates

| Gate | What is reviewed | Reviewer (role) | Blocks |
|:--|:--|:--|:--|
| G1 PoC result | p95 latency, cost and compression from [C-WL-01, own testing] on both shortlisted platforms | Lead Analytics Engineer + Retention analytics lead | Vendor selection |
| G2 ML hand-off | Feature extract time and point-in-time correctness from [C-WL-02, own testing] | ML Engineering lead | Vendor selection |
| G3 Cost | Projected compute + storage vs [A-BUD-01] / [A-BUD-02] at negotiated pricing | Finance (CFO delegate) | Contract signature |
| G4 Privacy | Data residency, `pii` tagging scheme, role model vs consent scope [A-GOV-01] | Privacy / Legal | Loading production PII |

## Evidence carried forward

| ID | Label | What it supports here | What it does not support |
|:--|:--|:--|:--|
| E-WL-01 | peer-reviewed | Elastic storage/compute separation fits a bursty 07:00 workload | Any cost figure for Aurora |
| E-WL-02 | peer-reviewed | The strongest case for Option B (one copy for BI + ML) | Neutral comparison; authors are lakehouse vendor founders |
| E-WL-03 | peer-reviewed | Direct ML reads of ACID tables (Option B) | Performance at Aurora's size |
| E-WL-04 | peer-reviewed | Open table formats reduce reversal cost; they add maintenance | Which format Aurora should use |
| E-WL-05 | peer-reviewed | Real BI workloads are many small queries, unlike TPC-DS | Which vendor wins on that pattern |
| E-WL-06 | vendor marketing | A vendor claims a price/performance lead | Anything at < 1 TB |
| E-WL-07 | vendor marketing | Vendor benchmarks are contested | Either vendor's numbers |
| E-WL-08 | practitioner benchmark | Untuned defaults are broadly competitive across warehouses | Aurora's query mix or pricing |
| E-WL-09 | practitioner benchmark | Directional single-table web-analytics performance | Joins, concurrency, cost |
| E-WL-10 | own testing | **Not yet run.** The only evidence that can pick the vendor | — |

**Plainly:** the choice between A and B rests on peer-reviewed mechanism papers plus Aurora's measured size; every vendor-performance claim is either vendor marketing or at the wrong scale. The vendor pick itself rests entirely on own testing that has not run yet.

**Claims requiring own testing before acceptance:** [C-WL-01, own testing], [C-WL-02, own testing].

## Sign-off

The storage section of the spec may not be written until every row below is signed.

| Role | Name | Decision | Date |
|:--|:--|:--|:--|
| Lead Analytics Engineer (owner) | | Pending | |
| ML Engineering lead | | Pending | |
| Retention team lead | | Pending | |
| Finance (CFO delegate) | | Pending | |
| Privacy / Legal | | Pending | |
