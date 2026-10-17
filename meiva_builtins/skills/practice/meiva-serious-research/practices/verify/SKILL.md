---
name: verify
description: "Audits research evidence and experimental results. Use when checking leakage, re-deriving metrics, or assessing whether a reported conclusion is warranted."
---

# Verification & Self-Audit

## One-sentence core

> Verification is an adversarial pass that assumes the result is wrong until proven otherwise: rule out leakage first, re-derive the key numbers from logs and code, audit every path that could have touched the test set, attribute failures before concluding, and label each claim with the strongest verification level it actually earned.

## Core principles

- **Leakage audit is the hard gate**: before believing any number, run the 8-category leakage audit — **Why**: a survey across 17 fields found leakage errors affecting 329 papers, some with "wildly overoptimistic" conclusions; in the civil-war prediction case, every "complex model beats logistic regression" claim failed to reproduce because of leakage (Kapoor & Narayanan) — **How**: work the taxonomy as a checklist — no independent test set, preprocessing before split, feature/target leakage, tuning on the test set, group overlap, augmentation across splits, cross-validation contamination, engineering leaks — and refuse to interpret results until every category is checked or explicitly waived with a recorded reason.

- **Re-derive key numbers from trace logs + code**: the strongest available verification is replaying the run and watching the number emerge — **Why**: replay is the strongest available check — the number emerges from the run, not from the write-up — and self-report is the weakest evidence about what actually happened; the 2026 measurement (55% detection from paper alone → 82% with logs + code, `ai-era/ai-failure-modes`) quantifies how much checkability depends on the trace — **How**: for each headline number, locate the run's command and log, rerun or replay it, and confirm the number; anything that cannot be rerun is marked [not rerun-verified] and treated as unverified.

- **Inventory the audited materials before the audit starts**: list every primary artifact the conclusion depends on — checkpoints, datasets, code versions, logs, environment pins — and verify existence before believing any re-derivation is possible — **Why**: an audit whose supporting materials are gone is a different activity (methodology review, not re-verification); field case 2026-08: a thesis causal conclusion lost its checkpoints (deleted months earlier), downgrading the re-audit from replay to methodological verification — knowing the degradation path up front prevents pretending the audit is stronger than the surviving record allows — **How**: at audit start write the material inventory (artifact path + existence check for ckpt/data/code-version/log); for each missing item, downgrade the achievable verification tier and say so in the report ("claim X could not be re-derived because artifact Y is missing; verified at rubric level only").
- **Audit p-hacking paths**: enumerate every decision that could have been made on the test set — **Why**: researcher degrees of freedom make significance a product of path choice, not evidence (Garden of Forking Paths); the mechanism scales to automated systems whose reward peeks at the test set — automated p-hacking (evidence: `ai-era/ai-failure-modes`) — **How**: walk the decision log: was any threshold, early stop, model selection, or run-dropping decision made after seeing test-set numbers? Post-hoc analyses are labeled exploratory; test-set decisions are zero-tolerance.

- **Adversarial self-review with four judges**: review your own work as four separate critics — correctness, faithfulness, consistency, completeness — **Why**: single-context review misses its own blind spots; independent adversarial perspectives are the standard cure in every reviewing institution, and the four-judge pattern is this skill's operational form (its pipeline evidence, from Grounded Autonomous Research, is in `ai-era/ai-research-landscape`) — **How**: run four passes, each asking one question: (1) are the numbers right and traceable? (2) does every claim match what the logs and sources actually say? (3) do the claims contradict each other or the evidence? (4) what would a hostile reviewer notice is missing — dropped runs, failures, unverified claims? Write at least one concrete finding per pass.

- **Attribute negative results before concluding**: a failure is not a verdict until its cause is identified — representation, optimization, data, or evaluation — **Why**: representable does not imply learnable: a method can fail because the hypothesis space cannot express the target or because the optimizer never found it (Domingos L11); misattribution turns a fixable problem into a verdict on the whole approach — **How**: run the attribution chain on every failure: can the model express the target (representation)? did the optimizer converge (optimization)? is the data adequate and clean (data)? does the metric measure what the claim asserts (evaluation)? For search-based systems use the optimization verification test: if Score(S*) > Score(S_out), the search failed; otherwise the scoring function is wrong (ML Yearning).

- **Pick the verification level by what is at stake**: choose the strongest available tier of the canonical ladder — formal verifier → process-level check → rubric review → self-assessment — **Why**: a claim that will change a decision deserves stronger verification than one that will not; and the weakest signal, self-assessment by the producer, is exactly what a single agent has by default, so a system that both produces and judges its own results is the systemic risk (renderer = decider). The ladder's 2026 formalization (RSI survey, 1,250 arXiv papers, 2024–2026) is in `ai-era/evaluation-paradigm-shift` — **How**: before concluding, ask in order: can this be formally checked (property test, type check, proof)? reproduced from logs? confirmed by a statistical test? If only self-assessment is available, say so and flag the claim for human or independent review.

## Checklist

- [ ] Leakage audit run against all 10 categories (incl. shortcut/protected-attribute and retrieval/system-level leakage) before any conclusion; waived categories carry a recorded reason; benchmark-based claims state provenance with contamination checked (test-set retrieval, train/test overlap) or marked unverified
- [ ] Every headline number re-derived from logs + code, or explicitly marked [not rerun-verified]
- [ ] Material inventory done before audit: every primary artifact (ckpt/data/code-version/log) checked for existence; missing artifacts recorded with the verification-tier downgrade they cause
- [ ] Decision log audited for p-hacking: no tuning, selection, or run-dropping decisions made after seeing test-set numbers
- [ ] Post-hoc analyses labeled exploratory; the pre-registered main analysis unchanged
- [ ] Four-judge pass completed (correctness / faithfulness / consistency / completeness), each with at least one concrete finding
- [ ] Negative results attributed (representation / optimization / data / evaluation) before being reported as verdicts
- [ ] Each claim labeled with its verification level; self-assessment-only claims flagged for human or independent review
- [ ] All runs and seeds accounted for; dropped runs have recorded reasons
- [ ] Citations verified against primary sources, including retraction status
- [ ] Model/algorithm comparison claims carry a significance test matched to the comparison type (McNemar / 5×2cv F / exact binomial); its absence is flagged
- [ ] If the test set was touched during tuning: it is demoted to dev set, a fresh test set is collected, or the claim is labeled exploratory until re-validated
- [ ] The audited claim is restated in the verifier's own words before judging (what exactly is being claimed)
- [ ] A decisive re-run is named: the single check that would overturn the verdict, and whether it was run
- [ ] The strongest objection the verifier can imagine has been stated and answered — or escalated to the human

## Relationships

- Root SKILL.md Quick Start C.4, D.3, and E (Interpreting results / Presenting / Auditing) — `../../SKILL.md`
- The leakage taxonomy is designed and prevented in `practices/design`; statistical tests and their assumptions in `practices/measure`; the trace you audit comes from `practices/reproduce`
- Ng-style error decomposition (bias / variance / data-mismatch) is the proactive form in `practices/measure`; this file's representation / optimization / data / evaluation attribution is the conclusion-time form
- Verification levels are declared to the human in `practices/present` — boundary: **present owns transparency and collaboration; verify owns trustworthiness**
- Integrity rules this pass enforces: `principles/honesty` (p-hacking, cherry-picking), `principles/falsifiability` (self-falsification), `principles/claim-evidence-alignment` (traceable numbers)
- AI-era failure modes to audit against: `ai-era/ai-failure-modes` (automated p-hacking, hallucinated citations)

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
