# ADR-003: Micro-batch ingestion of subscription events

**Status:** Proposed (Accepted only after gates G1–G4 below are signed)
**Date:** 2026-10-23
**Owner:** Lead Analytics Engineer, Subscription Health data product
**Decision area:** Streaming vs micro-batch
**Depends on:** ADR-001, ADR-002
**Door type:** Two-way. The event contract (IDs, event time, ingest time) is designed so a streaming path can later land into the same raw tables without changing downstream models.

> Citation key: `[E-xx-nn, label]` evidence row in `evidence/evidence_sources.csv`;
> `[A-xxx-nn]` assumption in `assumptions/assumptions.yaml`; `[C-xx-nn, own testing]`
> claim in `evidence/own_testing_claims.csv` (not yet run); `[projections]`
> `assumptions/derived/projections.csv`; `[judgement]` engineering judgement with no
> external evidence, called out so reviewers can challenge it.

## Context

- Subscription events are clickstream, billing/lifecycle webhooks and email/SMS engagement [A-EVT-02] [A-EVT-03].
- Volume at month 12: ~2.9M events/day in the base scenario and ~7.5M/day in the high scenario; a high-scenario promo day reaches ~37.6M events, about 435 events/second averaged over that day [projections] [A-EVT-04].
- Raw payload is ~2.7 GB/day base and ~10.5 GB/day high before compression [projections] [A-EVT-05].
- The only freshness requirement on record is the 07:00 daily SLA; no consumer has an intraday requirement today [A-OPS-02].
- Events arrive late (offline mobile, payment retries, webhook redelivery); incremental logic must reprocess a 3-day window [A-OPS-03] [C-BAS-03, own testing].
- Ingestion and streaming may use ~12% of the $180k/yr ceiling, ~$1.8k/month [A-BUD-01] [A-BUD-02]. Team is 3–5 people [A-OPS-01].
- Storage is a single warehouse [ADR-001]; scheduling, backfills and freshness checks run in the orchestrator [ADR-002].

## Options considered

### Option A: True streaming (event broker + stream processor, sub-minute freshness)

Events go to a managed broker; a stream processor writes continuously into the warehouse's raw tables with event-time windows and watermarks.

- **Genuine case for it:** enables in-session retention plays (e.g. a save offer while a subscriber is on the cancel page) if signal-to-cancel lag turns out to be minutes, which only [C-ST-01, own testing] can show.
- **Genuine case for it:** throughput is not a risk; commodity brokers sustain orders of magnitude more than Aurora's ~435 events/s peak-day average [E-ST-06, practitioner benchmark] [projections].
- **Genuine case for it:** mature engines with well-characterized latency/throughput behavior exist [E-ST-04, peer-reviewed] [E-ST-05, peer-reviewed].
- **Case against:** correctness under late and out-of-order data (windows, watermarks, triggers) is the hard part, and it is real work for a 3–5 person team [E-ST-02, peer-reviewed] [A-OPS-01] [A-OPS-03].
- **Case against:** always-on broker and processor capacity is paid 24/7 to serve a consumer that today reads once at 07:00 [A-OPS-02] [A-BUD-02] [judgement].

### Option B: Micro-batch (15-minute landing, hourly incremental models) (chosen)

A managed collector lands events in the warehouse every 15 minutes. dbt incremental models refresh hourly with a 3-day lookback; churn features build nightly.

- **Genuine case for it:** micro-batch achieves low latency and simple fault recovery by treating a stream as a series of small deterministic batches [E-ST-01, peer-reviewed].
- **Genuine case for it:** the same declarative API can run micro-batch or continuous, so starting with micro-batch does not foreclose streaming [E-ST-03, peer-reviewed].
- **Genuine case for it:** warehouse vendors offer managed low-latency ingest that fits [ADR-001] without new infrastructure [E-ST-08, vendor marketing].
- **Case against:** 15–60 minute staleness rules out in-session interventions [E-ST-02, peer-reviewed] [C-ST-01, own testing].
- **Case against:** promo-day bursts (3–5x average) concentrate in a few hours, and per-batch cost at that peak is unmeasured [A-EVT-04] [C-ST-02, own testing].

### Option C: Daily batch

A nightly load of the previous day's events, finished before the 07:00 build.

- **Genuine case for it:** it meets the only freshness SLA on record at the lowest cost and complexity [A-OPS-02] [A-OPS-01].
- **Genuine case for it:** late-data reprocessing is identical in every option, so daily batch loses no correctness [A-OPS-03] [E-ST-02, peer-reviewed].
- **Case against:** no same-day visibility on promo days, when volume runs 3–5x average and cancellation spikes are most expensive to miss [A-EVT-04] [judgement].
- **Case against:** a whole day's high-scenario promo volume (~37.6M events) lands in one window, putting the 07:00 SLA at risk on exactly the busiest days [projections] [A-OPS-02].

## Decision

- Adopt **Option B, micro-batch**: 15-minute landing cadence into raw tables, hourly incremental marts, nightly churn features, all scheduled by the orchestrator [ADR-002] [E-ST-01, peer-reviewed].
- Every event carries a stable `event_id` (idempotent merges), `event_time` and `ingested_at`. Incremental models reprocess the window in [A-OPS-03] each run, and [C-BAS-03, own testing] confirms the window [E-ST-02, peer-reviewed].
- Real-time, in-session retention plays are **out of scope** until [C-ST-01, own testing] shows signal-to-cancel lag short enough, and the retention team able to act fast enough, for sub-15-minute data to change outcomes.
- Accept only after the high-scenario promo-day replay in [C-ST-02, own testing] meets its lag and cost threshold.

## Strongest argument against

"You're deciding streaming is unnecessary before you've measured whether fast interventions save subscribers. If they do, you'll have built a second-class path and a culture that thinks in hours." This is fair: the evidence that freshness doesn't matter is absence of a requirement, not proof [A-OPS-02] [judgement]. That is why [C-ST-01, own testing] is a blocking gate, and why the event contract is designed so streaming can land in the same tables [E-ST-03, peer-reviewed].

## Consequences

**Easier**

- One ingestion pattern, scheduled and monitored like every other asset in [ADR-002]; no 24/7 streaming on-call for a 3–5 person team [A-OPS-01].
- Late and duplicate events are handled by one idempotent merge pattern rather than separate streaming and batch logic [E-ST-02, peer-reviewed] [A-OPS-03].

**Harder**

- Incremental merge and late-data logic carries real correctness risk. It is exactly the plausible-but-wrong change [ADR-004] routes to named human approval [ADR-004] [A-OPS-03].
- Dashboards are up to ~1 hour stale intraday; the retention team must be told this explicitly in the data product's SLA [A-OPS-02] [judgement].

### Reversal cost

| Item | Estimate | Basis |
|:--|:--|:--|
| Upgrade B → A: add broker + stream processor writing to the same raw tables | 4–8 engineer-weeks | [judgement] [E-ST-03, peer-reviewed] |
| Downstream dbt models | ~0 if the event contract holds | [judgement] |
| Ongoing cost of streaming vs micro-batch | Likely exceeds the ~$1.8k/month ingestion line; must be re-budgeted | [A-BUD-02] [judgement] |
| Downgrade B → C | < 1 engineer-week (change cadence) | [judgement] |

## Revisit triggers

- [C-ST-01, own testing] shows median signal-to-cancel lag under one hour and a retention play that can act within 15 minutes.
- Peak-day micro-batch lag exceeds 20 minutes p95 or ingestion spend exceeds its allocation [C-ST-02, own testing] [C-BAS-02, own testing].
- Measured late-arrival p99 exceeds the window in [A-OPS-03] [C-BAS-03, own testing].

## Human-review gates

| Gate | What is reviewed | Reviewer (role) | Blocks |
|:--|:--|:--|:--|
| G1 Freshness need | Signal-to-cancel lag analysis and intervention response times from [C-ST-01, own testing] | Retention team lead + Lead Analytics Engineer | Ruling streaming out of scope |
| G2 Peak load | Promo-day replay lag and cost from [C-ST-02, own testing] | Data Engineering lead | Production cut-over |
| G3 Event contract | `event_id`, `event_time`, `ingested_at`, late window, PII fields per source | Data Engineering lead + Privacy / Legal | First production source |
| G4 SLA publication | Written freshness SLA (07:00 daily, ~1 h intraday) acknowledged by consumers | Retention team lead + ML Engineering lead | Dashboard launch |

## Evidence carried forward

| ID | Label | What it supports here | What it does not support |
|:--|:--|:--|:--|
| E-ST-01 | peer-reviewed | Micro-batch gives low latency with simple recovery | That 15 minutes is enough for Aurora |
| E-ST-02 | peer-reviewed | The correctness/latency/cost trade-off is explicit and late data is the hard part | Aurora's own lag distribution |
| E-ST-03 | peer-reviewed | Starting micro-batch does not foreclose streaming | Migration effort for Aurora's stack |
| E-ST-04 | peer-reviewed | Streaming engines are mature at far higher rates than Aurora's | Cost at Aurora's rate |
| E-ST-05 | peer-reviewed | Method for measuring Aurora's own pipeline latency | Any result for Aurora |
| E-ST-06 | practitioner benchmark | Throughput is not the constraint | Freshness value or cost |
| E-ST-07 | vendor marketing | Upper-bound latency if a Spark-based path is ever used | Anything about the chosen warehouse path |
| E-ST-08 | vendor marketing | Managed in-warehouse ingest exists for Option B | Its cost or lag at Aurora's peak |
| E-ST-09 | own testing | **Not yet run.** Whether freshness changes retention outcomes | — |

**Plainly:** peer-reviewed work says micro-batch is technically sufficient and reversible. Whether streaming would *save subscribers* cannot come from any external source; it depends on Aurora's customers and playbooks, so this ADR stays Proposed until [C-ST-01, own testing] runs.

**Claims requiring own testing before acceptance:** [C-ST-01, own testing], [C-ST-02, own testing], [C-BAS-03, own testing].

## Sign-off

The ingestion section of the spec may not be written until every row below is signed. ADR-001 and ADR-002 must be signed first.

| Role | Name | Decision | Date |
|:--|:--|:--|:--|
| Lead Analytics Engineer (owner) | | Pending | |
| Retention team lead | | Pending | |
| Data Engineering lead | | Pending | |
| ML Engineering lead | | Pending | |
| Privacy / Legal | | Pending | |
