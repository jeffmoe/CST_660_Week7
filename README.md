# Aurora Nutrition — Subscription Health Data Product

**ADR set, data product specification, and a working proof artifact**

Aurora Nutrition is a direct-to-consumer supplements company growing from about 40,000 to
250,000 subscribers this fiscal year with roughly the same small data team. This repository
proposes the company's first formally owned data product, **Subscription Health**, which has
three consumers:

- **Retention team.** Churn monitoring dashboards and a daily at-risk list.
- **ML team.** A clean, point-in-time feature set for the churn-risk model.
- **Retention-support assistant.** Retrieval over redacted customer-service transcripts.

Every decision, rule and number in the repository traces back to one assumptions register
and one evidence table. Every document under `docs/` is generated from source files and
checked by a script, so the parts cannot quietly drift apart.

> **Reading time:** about 8–10 minutes. Each section ends with links to the full documents.

---

## At a glance

| Deliverable | What it contains | Start here |
|:--|:--|:--|
| **1. ADR set + evidence + compliance matrix** | Four architecture decisions with evidence-graded sources, reversal costs and human-review gates; security and privacy matrix | [`docs/adr/`](docs/adr/README.md), [`docs/evidence_table.md`](docs/evidence_table.md), [`docs/security_matrix.md`](docs/security_matrix.md) |
| **2. Data product specification** | Contracts and policy-as-code rules, MLOps handoff checklist, LLMOps/RAGOps plan, KPIs/SLAs, cost-aware operating plan | [`docs/contract_layer.md`](docs/contract_layer.md), [`docs/mlops_churn_handoff.md`](docs/mlops_churn_handoff.md), [`docs/ragops_plan.md`](docs/ragops_plan.md), [`docs/cost_operating_plan.md`](docs/cost_operating_plan.md) |
| **3. Working proof artifact** | Synthetic data with seeded violations, a contract validator that fails on them, and lineage | [`docs/validator_demo.md`](docs/validator_demo.md), [`docs/synthetic_validation_report.md`](docs/synthetic_validation_report.md), [`docs/lineage.md`](docs/lineage.md) |

**Headline numbers**

- **Size:** under 1 TB stored even in the high-growth scenario.
- **Cost:** about **$4,650 a month at 250k subscribers**, 31% of a $180k annual ceiling.
- **Pipeline checks:** 93/93 contract conformance checks and 45/45 synthetic validation checks pass.
- **Validator demonstration:** 16/16 cases match their expected pass/fail result.

---

## The starting point: assumptions and evidence

The **assumptions register** ([`docs/assumptions_register.md`](docs/assumptions_register.md))
holds 50 planning inputs, each with low, base and high values, an owner and a way to check it:

- subscriber growth curve;
- event volume per day;
- transcript volume;
- storage footprint;
- budget ceiling;
- regulatory scope;
- unit costs.

A script projects month-by-month volumes from it. The main takeaway is that this is a small-data
problem: about 2.9M events a day at base and roughly 125 GB stored at month 12 (900 GB in the
high scenario).

The **evidence table** ([`docs/evidence_table.md`](docs/evidence_table.md)) lists 47 sources,
each labeled with one evidence grade:

| Grade | Count | How it is weighed |
|:--|--:|:--|
| Peer-reviewed | 25 | Strong on how things work; rarely at Aurora's scale |
| Vendor marketing | 9 | Shows capability; performance and cost claims are treated as unverified |
| Practitioner benchmark | 7 | Directional; we note who ran it |
| Own testing | 6 | The only grade that can settle Aurora-specific claims; planned, not yet run |

Eighteen claims are explicitly **flagged as "own testing only"** because no outside source can
settle them. Each has a test method and a pass threshold. Examples: whether sub-15-minute data
would actually save subscribers, and whether redaction catches 99% of PII in our transcripts.

---

## Deliverable 1 — ADR set with evidence-graded sources and compliance matrix

### Architecture decisions ([`docs/adr/`](docs/adr/README.md))

| ADR | Decision | Real alternatives evaluated | Reversal cost |
|:--|:--|:--|:--|
| **001** Storage | Warehouse-centric (managed warehouse + dbt); feature tables exported in an open format | Lakehouse; hybrid warehouse + lake | About 6–10 engineer-weeks |
| **002** Orchestration | Asset-based orchestrator (Dagster), with **Airflow named as the fallback** | Airflow; warehouse-native scheduling only | About 3–5 engineer-weeks |
| **003** Ingestion | Micro-batch: 15-minute landing, hourly marts, nightly features | True streaming; daily batch | 4–8 engineer-weeks to move to streaming |
| **004** Agentic AI | Three zones: permitted, needs named human approval, prohibited | No agentic AI; broad autonomy | Under an hour to switch off; a bad merge costs 1–2 weeks |

Every ADR follows the same rules:

- **No strawman options.** Every rejected option states its strongest case, backed by evidence.
- **Every claim is cited** by evidence grade, for example `[E-WL-05, peer-reviewed]`. Claims
  without outside evidence are marked `[judgement]` so reviewers can challenge them.
- **Each ADR carries forward its evidence table,** saying what each source does and does not
  support.
- **Each ADR has named human-review gates and a sign-off table.**

The specification for an area may not be written until that ADR is signed off. A checker
(`scripts/check_adrs.py`) enforces all of this.

ADR-004 was written last on purpose. What agents may do depends on the first three decisions:

- the warehouse's `pii` tags set agent access limits;
- the orchestrator owns production runs;
- 3-day late-arriving data makes merge logic the riskiest code an agent could touch.

As a result, agents may open pull requests but never merge to production, and the assistant
drafts answers for staff but never acts on customer accounts.

### Compliance matrix ([`docs/security_matrix.md`](docs/security_matrix.md))

The matrix covers PII handling, consent, retention windows and access control:

- **Data classification:** five levels, from C0 Public to C4 Prohibited. Card numbers and CVVs
  must never be stored.
- **PII inventory:** 20 elements across subscription, transcript and billing data.
- **Consent model:** 9 purposes, with the US and GDPR legal basis for each.
- **Retention schedule:** 19 rules.
- **Access control:** 10 roles, plus 8 attribute-based policies covering masking, consent
  filters, location, deletion and small-group suppression.
- **Regulatory mapping:** CCPA/CPRA, other US state privacy laws, Washington's My Health My
  Data Act, GDPR, TCPA/CAN-SPAM, PCI DSS, tax and breach-notification rules.
- **Named review gates:** 8 gates covering cross-border transfers and re-identification risk.

**Retention is tied to regulatory scope.** The script works out which laws apply from the
jurisdiction assumptions, which Legal must confirm, then resolves each retention window from
them. For example, transcripts are kept 36 months under US-only scope and 24 months if EU/UK
customers come into scope.

---

## Deliverable 2 — Data product specification

### Policy-as-code rules and contracts ([`docs/contract_layer.md`](docs/contract_layer.md))

- **Consumer register.** Each of the three consumers has a contract: what it reads, under which
  roles and consent purposes, its SLA, what happens on breach, and how changes reach it.
- **Five schema definitions.** Raw events, the daily retention table, the hourly aggregate,
  churn features and transcript chunks. Every column records its type, whether it can be null,
  its classification and its PII link.
- **23 governance rules as code** (`aurora_contracts/governance.py`), each with a declared
  failure behavior:

| Rule | Example failure behavior |
|:--|:--|
| Schema drift | Block publish; consumers keep the last good version |
| Null / duplicate | Block for marts and features; quarantine rows for raw events |
| PII scan | Quarantine the row; a card number or SSN opens a **security incident** |
| Freshness | Serve stale data with a banner, then block after repeated misses |
| Consent scope | Quarantine out-of-scope subscribers; **missing consent fails closed** |

The contracts generate dbt model contracts (one per warehouse candidate, since ADR-001 picks
the vendor by proof-of-concept) and the producer event JSON Schema. 93 checks confirm the
contracts match ADR-001–003, the compliance matrix and the assumptions.

### MLOps handoff checklist ([`docs/mlops_churn_handoff.md`](docs/mlops_churn_handoff.md))

The checklist has 22 items, 19 of which block go-live. Examples:

- point-in-time correctness and a leakage review;
- consent exclusions applied, and no health-inferable features;
- training and test split by time;
- reproducible training and a tested rollback;
- nightly drift monitoring;
- a 10% outreach holdout to measure real save-rate uplift;
- a re-identification review before any export.

### LLMOps / RAGOps plan ([`docs/ragops_plan.md`](docs/ragops_plan.md))

- **Chunking:** split on speaker turns so a question stays with its answer; chunks of at most
  512 tokens with one turn of overlap; repeatable chunk IDs.
- **Refresh:** nightly upserts; deletions within 24 hours; full re-index only behind the
  evaluation gate.
- **Evaluation:**
  - a 200-question set from real retention staff questions, with a held-out test split;
  - an AI judge that must agree with human reviewers before use;
  - 11 release thresholds, such as finding the right passage in the top 5 at least 85% of the
    time, faithfulness at least 0.95 and zero PII leaks.
- **Drift detection:** shifts in query and corpus topics, retrieval match strength and the
  no-good-match rate.
- **Retrieval checks:** index counts reconciled, 20 fixed test questions with rollback, and a
  check that deleted subscribers' chunks never come back.

### KPIs and SLAs

| Measure | Target |
|:--|:--|
| Features and daily retention table ready | By 07:00 on ≥ 99% of days |
| Churn scores published | By 07:30 on ≥ 99% of days |
| Hourly intraday data | Typically about 1 h behind, at most 90 min (micro-batch) |
| Churn model quality | ROC-AUC ≥ 0.75 (placeholder), top-decile lift ≥ 3, calibration error ≤ 0.05 |
| Feature and score drift (PSI) | < 0.10 OK · 0.10–0.25 investigate · ≥ 0.25 retrain review |
| Assistant | Cost ≤ $0.05 per conversation, p95 latency ≤ 4 s, faithfulness ≥ 0.95 |
| Business outcome | Save-rate uplift against the holdout, reviewed quarterly |

### Cost-aware operating plan ([`docs/cost_operating_plan.md`](docs/cost_operating_plan.md))

| Scenario (month 12) | Subscribers | Per month | Share of $180k ceiling |
|:--|--:|--:|--:|
| Low | 180,000 | $1,558 | 10% |
| **Base** | **250,000** | **$4,645** | **31%** |
| High | 320,000 | $14,138 | 94% (LLM, orchestration and observability lines over allocation) |

- **Depends on ADR-003.** Micro-batch ingestion is priced in; streaming would add about
  $2,200 a month. A script fails if the cost model ever stops matching ADR-003 or the
  contracts.
- **Assistant cost.** About $0.043 per conversation on Claude Opus 5.5. A cheaper model is
  shown as an option, but only if it passes the same evaluation.
- **Sign-offs required.** The cost estimate (COST-G1) and the RAG evaluation method (RAG-G1)
  each need a named sign-off before budgets or pilots are committed.

---

## Deliverable 3 — Working proof artifact

The proof shows that the specification actually works when run against data.

1. **Synthetic data** (`scripts/generate_synthetic.py`). The generator simulates Aurora's
   source systems using the assumptions register. It builds all five contract datasets (about
   850k events by default) and seeds 16 edge cases into the *sources*, so they flow through the
   real transformations. Examples:
   - a consent-scope violation;
   - an email the redactor misses;
   - a card number in a transcript;
   - a duplicate webhook;
   - events arriving 2 and 5 days late.

   All personal data is fictitious.
2. **Contract validator** (`scripts/validate_contract.py`). It runs a dataset's governance rules
   from the contracts and **fails on violations**:

   | Exit | Meaning |
   |:--|:--|
   | 0 | Pass |
   | 1 | Rows quarantined |
   | 2 | Blocked |

3. **Demonstration** ([`docs/validator_demo.md`](docs/validator_demo.md)). Clean batches pass.
   Each seeded bad record, added back on its own, fails exactly as the contracts predict. The
   card-number case, for example:

   ```
   FAIL  GR-RAG-03  pii_scan  [ERROR] security_incident: PII found ... (Prohibited data: card_number)
   VERDICT: FAIL (BLOCKED)  (exit 2)  publish=False  publishable=0  quarantined=1
   ```

4. **Full validation and lineage.** [`docs/synthetic_validation_report.md`](docs/synthetic_validation_report.md)
   checks the late-data handling (recalculated independently), point-in-time labels, the
   MLOps/RAGOps code and calibration to the assumptions. [`docs/lineage.md`](docs/lineage.md)
   maps all 56 contract columns to their source fields and transformations.

**Findings the proof surfaced:**

- **Redaction gaps.** The simple redactor misses emails that aren't on the customer's profile
  and card numbers typed without spaces. The PII scan catches both.
- **Calibration bug, fixed.** Balanced class weights made churn scores unusable as
  probabilities.
- **Hourly table too sparse at small scale.** The intraday aggregate needs more subscribers
  before groups reach the minimum size of 10.

---

## What is not done yet

- **Sign-offs are pending.** All four ADRs, the compliance matrix, the cost estimate and the RAG
  evaluation method show as **blocked** until the named reviewers approve.
- **Own testing has not run.** The 18 "own testing only" claims are planned, not executed. Each
  ADR stays *Proposed* until its tests pass.
- **Placeholders remain.** These include the budget ceiling, unit costs (awaiting vendor
  quotes), the ROC-AUC floor and the time zone used for the 07:00 deadlines.
- **Legal confirmation is needed.** Jurisdiction assumptions are flagged ⚑ for Legal, notably
  whether supplement purchases count as consumer health data.
- **Synthetic scores aren't quality claims.** The proof artifact's model and retrieval scores
  show the code works; they say nothing about real-world quality.

---

## Run it

```bash
pip install -r requirements.txt
python scripts/build_assumptions.py       # assumptions register + projections
python scripts/build_evidence.py          # evidence table; checks grades and own-testing coverage
python scripts/check_adrs.py              # ADR citations, options, evidence carry-forward, sign-off gate
python scripts/build_security_matrix.py   # compliance matrix; retention resolved from regulatory scope
python scripts/build_contracts.py         # contract layer; conformance to ADR-001..003; dbt + JSON Schema
python scripts/build_mlops.py             # handoff, RAGOps and cost plans; coupling checks
python scripts/generate_synthetic.py      # synthetic data with seeded edge cases (deterministic per --seed)
python scripts/validate_synthetic.py      # validates the specs against it; writes report + lineage
python scripts/validate_contract.py features.churn_features --now 2027-03-10T08:00:00-06:00   # one dataset
python scripts/demo_validator.py          # demonstration against the seeded bad records
python -m pytest                          # 57 tests, including an end-to-end synthetic run
```

Every file in `docs/` is generated. Edit the source (`assumptions/`, `evidence/`, `security/`,
`contracts/`, `mlops/`, `synthetic/lineage.yaml`, `validator/`) and re-run the scripts.

## Repository map

| Path | Purpose |
|:--|:--|
| `assumptions/`, `evidence/` | Assumptions register, evidence table, own-testing claims |
| `docs/adr/` | ADR-001 to ADR-004 |
| `security/` | Compliance matrix source |
| `contracts/`, `aurora_contracts/` | Consumer register, schemas, governance rules, validator, generated dbt/JSON Schema |
| `mlops/`, `aurora_mlops/` | Handoff, RAGOps and cost plans; chunker, metrics, drift, gates, cost model |
| `synthetic/`, `aurora_synthetic/`, `validator/` | Lineage, synthetic generator, demonstration cases (generated data goes in `synthetic/data/`, gitignored) |
| `scripts/` | Build, check, generate and validate scripts |
| `tests/` | Test suite |

---

## AI assistance disclosure

This data product was developed **with the assistance of AI**. Anthropic's Claude, used
through Claude Code, helped draft the documents, ADRs, YAML specifications, Python code, tests
and this README, and helped find and check sources. The author set the scope and requirements
for each deliverable and is responsible for the final content.

Things to keep in mind when relying on this material:

- **Citations.** Some sources in the evidence table were confirmed by web search during
  drafting; others came from the model's own knowledge and should be spot-checked before
  external use.
- **Legal mapping.** The regulatory mapping is not legal advice and needs review by Legal.
- **Numbers.** Cost figures use list prices and placeholder unit costs. Synthetic-data results
  show that the specification works; they are not evidence of real-world performance.
