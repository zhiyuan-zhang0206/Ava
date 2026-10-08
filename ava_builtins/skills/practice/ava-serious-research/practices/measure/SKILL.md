---
name: measure
description: "Selects research metrics, uncertainty estimates, and statistical tests. Use when comparing models or algorithms or interpreting experimental differences."
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

## Relationships
- Negative-result attribution (representation / optimization / data / evaluation) happens at conclusion time in `practices/verify`; measure's error decomposition (bias / variance / data-mismatch) is the proactive form — two lenses on the same question, cite both
- Protocol and split design (which sets exist, when the test set is touched): `practices/design`
- Reproducing the seeds, configs, and environment behind each number: `practices/reproduce`
- Auditing whether a reported number survives re-derivation and which analyses were post-hoc: `practices/verify`
- Reporting the test, the correction, and the qualifiers to a human: `practices/present`
- Why the warrant matters (claim–evidence alignment) and why p-hacking is forbidden: `principles/claim-evidence-alignment`, `principles/honesty`
- Decision tree by dataset size × goal: `../../references/04-raschka.md`; error decomposition and optimizing/satisficing: `../../references/02-mlyearning.md`

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
