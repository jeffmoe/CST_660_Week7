# ADR-004: Permitted agentic AI automation and human-review gates

**Status:** Proposed (Accepted only after the shadow pilot and gates G0–G6 below)
**Date:** 2026-10-23
**Owner:** Lead Analytics Engineer, Subscription Health data product
**Decision area:** Agentic AI
**Depends on:** ADR-001, ADR-002, ADR-003
**Supersedes draft:** "Allow an agentic AI process to open pull requests against transformation code" (2026-10-21), folded into Zone 2 below.
**Door type:** Mixed. Permissions are two-way: revoking an agent token or turning off the assistant takes an hour. A bad merge is closer to one-way: corrupted incremental history or a broken feature definition means a backfill, a reconciliation freeze and a churn-model retrain. Sending transcripts to a third-party provider cannot be undone.

> Citation key: `[E-xx-nn, label]` evidence row in `evidence/evidence_sources.csv`;
> `[A-xxx-nn]` assumption in `assumptions/assumptions.yaml`; `[C-xx-nn, own testing]`
> claim in `evidence/own_testing_claims.csv` (not yet run); `[projections]`
> `assumptions/derived/projections.csv`; `[judgement]` engineering judgement with no
> external evidence, called out so reviewers can challenge it.

This ADR is written last because the permitted zones are bounded by what ADR-001 to ADR-003 decided: where data lives and how access is controlled, what owns schedules and checks, and how fresh and how late the data is.

## Context

- Two agentic uses are proposed: (1) a **retention-support assistant** that answers retention staff using retrieval over customer service transcripts [E-AI-01, peer-reviewed], and (2) an **agent that opens pull requests** against dbt models and orchestration code [E-AI-12, peer-reviewed].
- Transcript volume grows from a ~60k backfill to ~208k (base) or ~354k (high) transcripts by month 12, adding 25k–48k per month [projections] [A-TRN-01] [A-TRN-03].
- At one assistant conversation per support contact and ~$0.05 per conversation, month-12 LLM spend is ~$1.25k (base) and ~$7.2k (high) per month against a ~$2.25k/month allocation, so the high scenario is over budget [projections] [A-BUD-02] [A-BUD-03].
- The team stays at 3–5 people while subscribers grow ~6x. Review time is the scarcest resource and any gate that adds review load has to be measured [A-OPS-01] [A-GRW-02].
- Consent language covers fulfillment and service communications only and says nothing about third-party processing; sending transcripts or subscriber rows to an external model provider is unconfirmed [A-GOV-01].

**What ADR-001 to ADR-003 fix for this decision**

- [ADR-001] puts all data in one warehouse with a `pii` tag on every table. Agent access can therefore be bounded with warehouse roles: agent identities get no grant on `pii=true` tables and work against a synthetic-data schema [ADR-001].
- [ADR-002] makes the orchestrator own schedules, backfills and blocking asset checks. Those checks become the policy-as-code tests an agent's PR must pass, and agents never trigger production runs [ADR-002].
- [ADR-003] makes data up to ~1 hour stale intraday, with a 3-day late-arriving window handled by idempotent incremental merges. An agent acting on a customer in real time would act on stale state, and late-data merge logic is the highest-risk code an agent could touch [ADR-003] [A-OPS-03].

## Options considered

### Option A: No agentic automation

The assistant is a fixed retrieve-then-generate workflow with no tool use, and humans write all transformation code. AI coding assistance stays in individual editors.

- **Genuine case for it:** a model vendor itself recommends starting with the simplest workflow and adding autonomy only when it measurably helps [E-AI-09, vendor marketing].
- **Genuine case for it:** agentic controllers are harder to debug and their errors cascade through modular pipelines [E-AI-11, peer-reviewed].
- **Genuine case for it:** a randomized trial found experienced developers 19% slower with AI tools while believing they were faster, so expected productivity gains may not appear [E-AI-13, practitioner benchmark].
- **Case against:** with a flat 3–5 person team facing ~6x growth, docs, tests and model scaffolding fall behind without some leverage [A-OPS-01] [A-GRW-02] [judgement].
- **Case against:** it forgoes measuring whether bounded agents help, which the vendor evidence suggests is at least worth testing [E-AI-14, vendor marketing].

### Option B: Bounded zones with human-review gates (chosen)

Agentic actions are sorted into three zones (permitted, approval-required, prohibited) derived from ADR-001 to ADR-003. Each zone has a named gate.

- **Genuine case for it:** grounding answers in retrieved transcripts reduces fabricated statements in dialogue, so a read-only assistant has a peer-reviewed safety basis [E-AI-02, peer-reviewed] [E-AI-01, peer-reviewed].
- **Genuine case for it:** agents can resolve some real repository issues, so opening PRs for routine work is plausible [E-AI-12, peer-reviewed].
- **Genuine case for it:** gates aimed at the riskiest code paths put the scarce review time where errors cascade [E-AI-11, peer-reviewed] [A-OPS-01].
- **Case against:** review load moves rather than disappears, and the evidence for agent productivity on dbt/SQL work is vendor marketing only [E-AI-14, vendor marketing] [E-AI-13, practitioner benchmark].
- **Case against:** SWE-bench workloads are Python issues, not SQL with incremental and late-data semantics, so its results do not transfer to our code [E-AI-12, peer-reviewed] [A-OPS-03].

### Option C: Broad autonomy

The assistant can take account actions (apply discounts, pause or cancel subscriptions) and message customers; the code agent auto-merges to production when CI passes.

- **Genuine case for it:** tool-using reason-and-act loops are an established pattern and give the most leverage per engineer [E-AI-04, peer-reviewed] [A-OPS-01].
- **Genuine case for it:** customer-service agent benchmarks exist for exactly these retail actions, so capability can be measured [E-AI-07, practitioner benchmark].
- **Case against:** the same benchmark shows agents are inconsistent across repeated trials (low pass^k), which is unacceptable for actions on customer accounts and money [E-AI-07, practitioner benchmark].
- **Case against:** agent evaluations that ignore cost overstate value, and cost is already over allocation in the high scenario [E-AI-08, practitioner benchmark] [projections].
- **Case against:** customer-facing actions on data up to ~1 hour stale, and processing that consent may not cover, create risk no CI test can catch [ADR-003] [A-GOV-01].

## Decision

Adopt **Option B, bounded zones with human-review gates**. The assistant starts as a **fixed retrieval workflow**; it becomes tool-using only if [C-AI-03, own testing] shows a gain of at least 10 points with no loss of reliability at no more than twice the cost [E-AI-07, practitioner benchmark] [E-AI-08, practitioner benchmark].

### Zone 1: Permitted (logged, sampled review, no per-action approval)

| Action | Bound set by | Gate |
|:--|:--|:--|
| Assistant retrieves **redacted** transcripts and drafts answers, talk tracks or summaries for a retention staff member, who decides what, if anything, reaches the customer | [ADR-001] warehouse roles; [A-GOV-01]; [E-AI-02, peer-reviewed] | G0, G1; weekly faithfulness sample [C-AI-02, own testing] |
| Code agent opens PRs for docs, column descriptions, new tests, and new non-contract models in development | [ADR-002] checks as policy tests; [E-AI-12, peer-reviewed] | G2, G4 |
| Agent-authored PRs auto-merge **to staging only** after all policy-as-code tests pass | [ADR-002] asset checks | G2 |

### Zone 2: Permitted only with named human approval recorded in the PR or CRM

| Action | Why it is here | Approver (role) |
|:--|:--|:--|
| Any merge of an agent-authored PR to **production** | Plausible SQL bugs pass schema tests [E-AI-12, peer-reviewed] [C-AI-05, own testing] | Analytics engineer other than the requester |
| PR touching a `pii=true` table or column | [ADR-001] tagging; [A-GOV-01] | Analytics engineer + Privacy reviewer |
| PR altering a primary key, `event_id` handling, or a stable-contract field of the churn feature tables | Breaks training/serving parity for the ML team [E-OR-03, peer-reviewed] [ADR-001] | ML Engineering lead |
| PR editing incremental merge, lookback or late-arriving-data logic | Highest-risk code from [ADR-003]; errors double-count silently [A-OPS-03] | Data Engineering lead |
| PR editing orchestration schedules, backfill jobs, freshness policies or check thresholds | Owned by the orchestrator per [ADR-002]; affects the 07:00 SLA [A-OPS-02] | Lead Analytics Engineer |
| Assistant **proposes** a retention offer (discount, pause, plan change) | Account and money actions need a human decision [E-AI-07, practitioner benchmark] | Retention staff member applies it in the CRM |

### Zone 3: Prohibited

| Action | Reason |
|:--|:--|
| Any agent merging to production, or approving its own or another agent's PR | Removes the only check on plausible-but-wrong changes [E-AI-11, peer-reviewed] [C-AI-05, own testing] |
| Any agent executing account actions or sending messages directly to customers | Inconsistent across trials [E-AI-07, practitioner benchmark]; acts on data up to ~1 h stale [ADR-003] |
| Any agent reading production subscriber rows; sending unredacted transcripts to a third-party provider | Consent does not cover third-party processing until G0 clears [A-GOV-01] |
| Any agent triggering production runs or backfills, or writing directly to churn feature tables | Orchestrator-owned per [ADR-002]; feature integrity per [E-OR-03, peer-reviewed] |
| Any agent deleting from the vector store (including right-to-erasure purges) | Deletion must be a deterministic, audited pipeline job [ADR-002] [judgement] |
| Any agent changing its own permissions, the policy tests, or evaluation thresholds | Would let the gate be weakened by the thing it gates [judgement] |

## Strongest argument against (the skeptical VP of Engineering)

"You haven't removed work, you've moved it into review, and review is your scarcest resource at 6x scale. Agent SQL looks plausible, and the dangerous bugs are the plausible ones: a merge that double-counts events arriving three days late, or a new `subscription_status` level that quietly breaks training/serving parity. Both pass schema tests and only show up as a wrong churn dashboard on Monday. Tired reviewers rubber-stamp green builds, so your 'human gate' becomes a signature, not a check. And if anyone loosens the redaction rule, the assistant becomes third-party processing our consent terms never authorized."

Each part has support: review load [A-OPS-01], late-data risk [A-OPS-03] [ADR-003], debugging difficulty [E-AI-11, peer-reviewed], perceived-versus-real productivity [E-AI-13, practitioner benchmark], consent [A-GOV-01]. The response is not to argue it away but to measure it: the shadow pilot [C-AI-05, own testing] seeds exactly these bugs and fails the ADR if reviewers miss more than one in five.

## Consequences

**Easier**

- Retention staff get grounded answers from ~200k–350k transcripts instead of searching the helpdesk by hand [projections] [E-AI-02, peer-reviewed].
- Routine docs, tests and model scaffolding can keep pace with ~6x growth, and every agent change has a PR audit trail [A-GRW-02] [judgement].

**Harder**

- Review load goes up and must be tracked; [C-AI-05, own testing] caps added review at 4 hours per engineer per week [A-OPS-01].
- CI policy tests and the orchestrator's checks now carry real weight; their failures become incidents [ADR-002] [E-OR-02, peer-reviewed].
- A redaction pipeline, an evaluation harness, and a synthetic-data environment must be built and kept current before either use goes live [C-AI-01, own testing] [C-AI-02, own testing] [judgement].
- High-scenario LLM spend (~$7.2k/month) is ~3x its allocation; either cost per conversation drops or the budget is rebalanced [projections] [C-AI-04, own testing] [A-BUD-02].

### Reversal cost

| Item | Estimate | Basis |
|:--|:--|:--|
| Revoke code-agent repo token; disable assistant feature flag | < 1 hour | [judgement] |
| Undo a bad agent merge that reached production incremental models | Backfill of the affected window + reconciliation freeze + churn-model retrain: ~1–2 engineer-weeks per incident | [A-OPS-03] [ADR-002] [judgement] |
| Re-embed the corpus with a different (e.g. self-hosted) model if G0 later restricts the provider | ~350k transcripts in the high scenario; days of compute plus re-running [C-AI-01, own testing] | [projections] [A-TRN-05] |
| Data already sent to a third-party provider | **Cannot be reversed.** Hence G0 blocks first use | [A-GOV-01] |

## Supersede triggers

Write a successor ADR that cuts the code agent back to suggestion-only (no PRs) if **any one** of these happens:

1. Of any rolling 30 agent PRs that passed CI and merged, **more than 3 are reverted or hotfixed within 14 days** [judgement].
2. **One** agent-authored change reaches production touching a `pii=true` column or causes a miss on the 07:00 SLA [A-OPS-02] [ADR-001].
3. During the pilot, reviewers catch **fewer than 4 of 5 seeded bugs**, meaning the gate is a rubber stamp [C-AI-05, own testing].

Pull the assistant back to Zone 3 (off) if weekly faithfulness drops below 0.95, any PII leak is confirmed, or cost per conversation exceeds $0.05 for two consecutive months [C-AI-02, own testing] [C-AI-04, own testing] [A-BUD-03].

The trigger can also loosen the rules: after 100 merged agent PRs with zero reverts, write a successor ADR that considers wider auto-merge [judgement].

## Human-review gates

| Gate | What is reviewed | Reviewer (role) | Blocks |
|:--|:--|:--|:--|
| G0 Consent and provider | Whether a processor agreement covers LLM inference on redacted transcripts, or whether consent must change [A-GOV-01]; provider retention terms | Privacy / Legal | **Any** transcript or subscriber-derived text reaching an external model |
| G1 Assistant evaluation | Retrieval recall, faithfulness, PII redaction recall [C-AI-01, own testing] [C-AI-02, own testing] | ML Engineering lead + Retention team lead | Assistant pilot with retention staff |
| G2 Code-agent shadow pilot | Seeded-bug catch rate and review minutes [C-AI-05, own testing] | Lead Analytics Engineer + Data Engineering lead | Agent PRs on the real repository |
| G3 Agentic vs workflow | Task success, pass^k and cost of tool-use vs fixed workflow [C-AI-03, own testing] | ML Engineering lead | Giving the assistant any tools |
| G4 Per-PR approval | Every Zone 2 change and every production merge, approver named in the PR | Roles in the Zone 2 table | Merge |
| G5 Cost | Measured cost per conversation vs [A-BUD-03] and the LLM allocation [C-AI-04, own testing] | Finance (CFO delegate) | Scaling past the pilot |
| G6 Zone review | Quarterly review of zone tables, supersede triggers and incident log | Head of Data | Continued operation |

## Evidence carried forward

| ID | Label | What it supports here | What it does not support |
|:--|:--|:--|:--|
| E-AI-01 | peer-reviewed | The RAG pattern behind the assistant | Quality on Aurora transcripts |
| E-AI-02 | peer-reviewed | Retrieval grounding reduces hallucination in dialogue (Zone 1 basis) | Zero hallucination; PII safety |
| E-AI-03 | peer-reviewed | Tight top-k retrieval over stuffing whole transcripts | A specific chunk size |
| E-AI-04 | peer-reviewed | Tool-using agents are an established pattern (Option C, G3) | That they are reliable enough for account actions |
| E-AI-05 | peer-reviewed | Metrics for the evaluation harness (G1) | Thresholds for Aurora |
| E-AI-06 | peer-reviewed | Shortlisting embedding models | Recall on support-ticket language |
| E-AI-07 | practitioner benchmark | Agents are inconsistent across trials in customer-service tasks (Zone 3 basis) | Aurora-specific success rates |
| E-AI-08 | practitioner benchmark | Evaluate accuracy and cost together (G3, G5) | Any cost figure for Aurora |
| E-AI-09 | vendor marketing | Start with workflows; add autonomy when measured (Option A, default workflow) | That agents would or would not help Aurora |
| E-AI-10 | own testing | **Not yet run.** Eval harness, red-team set, cost pilot, shadow pilot | — |
| E-AI-11 | peer-reviewed | Agentic controllers are harder to debug; errors cascade; leakage is real. Supports the gates | The productivity case. Covers RAG, not SQL generation |
| E-AI-12 | peer-reviewed | Agents can resolve some real repository issues | dbt/SQL with incremental semantics |
| E-AI-13 | practitioner benchmark | Perceived AI productivity can be the opposite of measured productivity | Results for less experienced devs or unfamiliar code |
| E-AI-14 | vendor marketing (weakest) | That code agents are worth piloting | Planning; no workload shape |
| — | practitioner benchmark | **Empty.** No trusted benchmark of agents on a described dbt/SQL workload was found | — |

**Plainly:** the productivity case rests on vendor marketing, the weakest tier, and is contradicted by the one randomized trial. The safety case rests on peer-reviewed work that only partly fits our workload. That is why this ADR stays *Proposed* until our own testing closes the gap.

**Claims requiring own testing before acceptance:** [C-AI-01, own testing], [C-AI-02, own testing], [C-AI-03, own testing], [C-AI-04, own testing], [C-AI-05, own testing].

## Sign-off

The agentic-AI section of the spec may not be written until every row below is signed. ADR-001, ADR-002 and ADR-003 must be signed first, because the zones above depend on them.

| Role | Name | Decision | Date |
|:--|:--|:--|:--|
| Lead Analytics Engineer (owner) | | Pending | |
| Head of Data | | Pending | |
| ML Engineering lead | | Pending | |
| Retention team lead | | Pending | |
| Privacy / Legal | | Pending | |
| VP Engineering (skeptic of record) | | Pending | |
