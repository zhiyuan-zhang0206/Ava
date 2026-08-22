---
name: measure
description: Use when defining an evaluation metric, comparing models or algorithms, interpreting a difference between two numbers, or deciding what to measure next — choose metrics whose warrant matches the claim, report uncertainty honestly, and run the statistical test that matches the comparison type.
---

# Measure and Statistics

## One-sentence core
> A number supports a claim only when the metric measures what the claim asserts (warrant), the estimate carries its uncertainty, and the statistical test matches the comparison type — otherwise the difference you see is indistinguishable from noise, or is an artifact of the analysis path you happened to take.

## Core principles

- **Metric warrant before computation**: for every headline number, write down what it measures and why that supports the claim, before any run — **Why**: metric substitution (claiming robustness while reporting accuracy) is the most insidious mismatch in evaluation, and errors in the evaluation method are harder to spot than errors in the model itself (Raschka); Kapoor & Narayanan show that inflated metrics in ML-based science are most often leakage artifacts, which a warrant check catches early — **How**: write the triplet "claim → metric → warrant" in the experiment record before writing code, and name who scored the metric (the model under test, a third-party judge, or yourself); if you cannot say what the metric measures in the claim's own terms, pick another metric.
- **One headline number, explicit trade-offs**: declare exactly one optimizing metric and treat the rest as satisficing (threshold) metrics — **Why**: without a single-number metric, precision/recall-style trade-offs stall decisions; with N dimensions, keeping N−1 satisficing and 1 optimizing makes the trade-off explicit (Ng, ML Yearning) — **How**: every experiment report names its optimizing metric, the satisficing metrics with thresholds, and why that ordering matches the research question.
- **Point estimates carry their uncertainty**: report mean ± std across seeds/folds with the repetition count, never a bare number — **Why**: a single point estimate cannot distinguish a real difference from noise; no error bar means no comparison is possible (Raschka: repeated stratified CV, mean ± std across folds/repeats) — **How**: run at least 3, preferably 5–10 seeds (or repeated k-fold); report the full distribution, not the best seed; n=1 must be labeled n=1.
- **Test matched to comparison type**: model comparison on one test set uses McNemar (exact binomial when the discordant count B+C < 50); algorithm comparison across data fluctuation uses the 5x2cv protocol (Alpaydin's combined F-test preferred, Dietterich's paired t as fallback) — **Why**: the proportion z-test violates independence on a shared test set and the resampled paired t-test violates it across overlapping folds — both inflate the type-I error rate severely (Dietterich 1998; Raschka) — **How**: pick from Raschka's decision table (dataset size × goal → protocol → test); report the statistic, its degrees of freedom, the p-value, and the validity condition (e.g. B+C ≥ 50 for McNemar).
- **Control multiple comparisons**: with ≥3 models, run an omnibus test (Cochran's Q, a non-parametric generalization of McNemar) first, and apply Bonferroni correction to any pairwise follow-up — **Why**: 5 algorithms produce C(5,2)=10 pairwise comparisons; at α=0.05 each, the chance of at least one false positive is 1−0.95¹⁰ ≈ 40.1% (Raschka) — **How**: α_adj = α/m for m comparisons; report the corrected threshold; if the omnibus is not significant, do not report pairwise winners.
- **Pre-register the primary analysis**: write hypothesis, metric, procedure, and the decision rule before running; any later analysis is labeled exploratory — **Why**: a dataset admits many defensible analysis paths, and significance is often an artifact of which path was chosen after seeing the data (Gelman & Loken, Garden of Forking Paths); the same mechanism scales to automated systems whose reward looks at the test set (evidence: `ai-era/ai-failure-modes`) — **How**: the experiment record contains a one-line prediction and the pre-registered criterion ("component A is removed → gap < 1 point → hypothesis fails"); post-hoc analyses are written up as exploratory, never as confirmatory.
- **Decompose errors before choosing the next step**: categorize failures quantitatively on an eyeball-only subset, compute each category's ceiling, and diagnose bias vs variance vs data mismatch before spending resources — **Why**: decisions made from a few impressionistic examples are the classic waste; a category covering 5% of errors can at most remove 5% of total error (Ng); whether to add data or capacity depends on the bias/variance diagnosis, not intuition — **How**: keep an Eyeball dev subset (human-readable, for error analysis) and a Blackbox dev subset (automated evaluation only, never eyeballed); sample ~100 errors, tabulate categories, compute the ceiling of each; separate train and train-dev errors to attribute the gap to variance vs data mismatch; draw learning curves (train/dev error vs data size) before concluding "more data" or "bigger model".

## Checklist
- [ ] Class imbalance reported with the headline number: per-class metrics accompany any aggregate metric on skewed data
- [ ] Warrant written for the headline metric before any run: what it measures, why it supports the claim
- [ ] Exactly one optimizing metric declared; satisficing metrics listed with thresholds
- [ ] Comparison type declared (performance estimation / model selection / algorithm comparison) and the protocol matches it
- [ ] Multiple seeds or repeated folds run; mean ± std and repetition count reported; no single-run comparison
- [ ] Model comparison uses McNemar (or exact binomial when B+C < 50) with B+C reported; proportion z-test not used
- [ ] Algorithm comparison uses 5x2cv (Alpaydin F or Dietterich t); resampled paired t-test not used
- [ ] With ≥3 models: omnibus (Cochran's Q) run first; pairwise tests Bonferroni-corrected and the correction reported
- [ ] Primary analysis pre-registered before runs; every post-hoc analysis labeled exploratory
- [ ] Error analysis done on the Eyeball subset only, with counts and ceilings; test/Blackbox sets never eyeballed
- [ ] Learning curve (train/dev error vs data size) produced whenever "add data" or "increase capacity" is a candidate next step

## Anti-patterns
- **Metric substitution**: claim is about robustness, evidence is accuracy — → Instead: write the claim first, then require the metric to measure the claim's object (see `principles/claim-evidence-alignment`).
- **Cherry-picked seeds**: 10 seeds run, the best 3 reported as "±std" — → Instead: report the full distribution; dropped runs carry a reason.
- **Wrong test for the claim**: a McNemar p-value used to claim "algorithm A is better on this problem" — → Instead: McNemar supports only "model instance A beats B on this test set"; algorithm-level claims need 5x2cv.
- **Uncorrected pairwise fishing**: 10 pairwise tests at α=0.05 after the omnibus — → Instead: Bonferroni (α/m) on all follow-ups, or report effect sizes with corrected intervals.
- **Post-hoc hypotheses**: deciding what to measure after seeing the results — → Instead: pre-registered primary analysis; post-hoc labeled exploratory (same root as HARKing, `principles/honesty`).
- **Eyeballing the test set**: repeatedly inspecting test or Blackbox dev samples during debugging — → Instead: human analysis confined to the Eyeball subset; test set evaluated once.

## Bad → good
- **bad**: "Our method is more robust (accuracy 92.3%)." — no warrant, single run, robustness claim with accuracy evidence.
- **good**: "Our method is more robust to input perturbation: mean performance drop 4.1% (±0.8, 10 seeds) across 5 perturbation types vs 9.3% for the baseline. Accuracy is 92.3%, but this claim concerns the drop under perturbation, not overall accuracy. Model comparison on the shared test set: McNemar χ²=6.2 (B+C=214), p=0.013."
- **bad**: 5 algorithms compared with 10 pairwise McNemar tests at α=0.05; two "significant" winners reported.
- **good**: Cochran's Q over all 5 models first (Q=18.4, df=4, p=0.001); pairwise McNemar follow-ups with Bonferroni α_adj=0.005; only comparisons below the adjusted threshold reported as wins, others reported as inconclusive.
- **bad**: a dev error of 20% vs training error of 3% interpreted as "the model is bad" — followed by blind architecture changes.
- **good**: the gap is diagnosed: train 3% vs train-dev 4% vs dev 20% → the jump at train-dev→dev indicates data mismatch (per Ng's error-chain); learning curves show both curves flat → more data from the current distribution will not help; action: collect dev-distribution data, not more model capacity.

## Relationships
- Negative-result attribution (representation / optimization / data / evaluation) happens at conclusion time in `practices/verify`; measure's error decomposition (bias / variance / data-mismatch) is the proactive form — two lenses on the same question, cite both
- Protocol and split design (which sets exist, when the test set is touched): `practices/design`
- Reproducing the seeds, configs, and environment behind each number: `practices/reproduce`
- Auditing whether a reported number survives re-derivation and which analyses were post-hoc: `practices/verify`
- Reporting the test, the correction, and the qualifiers to a human: `practices/present`
- Why the warrant matters (claim–evidence alignment) and why p-hacking is forbidden: `principles/claim-evidence-alignment`, `principles/honesty`
- Decision tree by dataset size × goal: `../../references/04-raschka.md`; error decomposition and optimizing/satisficing: `../../references/02-mlyearning.md`

## Sources
- Raschka, *Model Evaluation, Model Selection, and Algorithm Selection in Machine Learning* (arXiv:1811.12808) — three goals, McNemar / 5x2cv / Cochran's Q + Bonferroni, decision table (`../../references/04-raschka.md`)
- Ng, *Machine Learning Yearning* — single-number metric, optimizing/satisficing, Eyeball/Blackbox dev sets, error analysis ceilings, bias/variance/data-mismatch decomposition, learning curves (`../../references/02-mlyearning.md`)
- Gelman & Loken, *The Garden of Forking Paths* — researcher degrees of freedom
- Kapoor & Narayanan, *Leakage and the Reproducibility Crisis in ML-based Science* — inflated metrics as leakage artifacts (`../../references/05-kapoor.md`)
- Luo, Kasirzadeh, Shah, CMU evaluation of AI Scientist (arXiv:2509.08713) — automated p-hacking via test-set-aware reward — evidence also in `ai-era/ai-failure-modes`
- Dietterich (1998), approximate statistical tests for comparing supervised classification learning algorithms — cited via Raschka
