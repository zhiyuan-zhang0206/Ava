---
name: design
description: Designs leakage-resistant experiments with sealed test sets, strong baselines, controlled axes, and claim-matched protocols. Use before writing training or evaluation code, choosing data splits, or accepting an experimental setup.
---

# Experimental Design

## One-sentence core
> Design decides results before any code runs: split before you process, seal the test set, change one axis at a time against strong baselines, and choose the protocol from your conclusion type and data size — then run the leakage audit before believing any number.

## Core principles
- **Split first, process after; seal the test set**: — **Why**: Kapoor & Narayanan's survey of 329 papers across 17 fields found leakage to be the head cause of the ML reproducibility crisis; the most common error is `fit_transform` on the full dataset before `train_test_split`. "Split first, then process; seal the test set until the final evaluation" is their first-line defense. — **How**: The split is the first line of the pipeline, before any statistic (mean, imputation, PCA, feature selection, augmentation); the test set is hashed and archived, physically unreachable from training code, and touched exactly once at final evaluation.
- **One axis at a time — representation, evaluation, optimization**: — **Why**: Domingos's Lesson 1: every ML algorithm is Representation + Evaluation + Optimization; change two axes at once and the result cannot be attributed to either — "a stronger model" and "a better optimizer" impersonate each other. — **How**: Name all three components in the experiment config; every comparison differs in exactly one; every gain must answer "does this come from representation, evaluation, or optimization?"
- **Strong baselines and neutral comparison**: — **Why**: Boulesteix's plea for neutral comparison studies; and Kapoor & Narayanan's civil-war-prediction reproduction: every paper claiming complex ML beat logistic regression failed to reproduce — the complex models did not beat decades-old LR. Weak baselines manufacture wins. — **How**: The baseline is a tuned strong simple method (logistic regression, linear model, rule baseline) under the identical protocol — same splits, same preprocessing, same tuning budget; report both learning curves, not just the final gap.
- **Ablate every component (full / -A / -B / -A-B)**: — **Why**: Without ablation, "we added A, B, C and it helped" cannot say which component earns its place (Domingos L1 attribution; the parsimony principle: complexity must be justified by evidence). — **How**: Every modular method ships an ablation table; a component survives only if removing it costs a significant, pre-registered amount.
- **Group-aware and time-aware splits**: — **Why**: Kapoor taxonomy categories 5 and 7: non-independent samples (same patient, device, session, cluster across splits) and cross-validation contamination are among the most silent leakages — random K-fold on grouped data inflates results because the model has seen the group. — **How**: Before splitting, inventory hidden groups (patient/cluster/device/timestamp); dedupe first; use GroupKFold for grouped data, TimeSeriesSplit for temporal data; the test set contains no group IDs seen in training.
- **Protocol from the decision card — conclusion type × data size (Raschka Fig. 23)**: — **Why**: Raschka: evaluation is goal-driven protocol selection — performance estimation, model selection, and algorithm comparison need different protocols and tests; every layer of mismatch biases conclusions optimistic, and errors in the evaluation method are harder to spot than errors in the model. — **How**: Declare the conclusion type in one line, then pick the cell from the card below; write the chosen cell into the design doc before coding.
- **Pre-register the comparison**: — **Why**: The Garden of Forking Paths: without a pre-registered hypothesis, metric, and decision rule, the analysis path is chosen after seeing results — the machinery of p-hacking (`principles/honesty`). — **How**: One dated paragraph: "we predict X; support = effect ≥ δ under test T; refutation = effect < δ." The design doc also records the primary metric and the protocol cell.

## Protocol decision card (Raschka Fig. 23, condensed)

| Data size | Performance estimation | Model selection | Model comparison | Algorithm comparison |
|---|---|---|---|---|
| Large (10k+) | 2-way holdout + CI | 3-way holdout (train/val/test) | McNemar (exact binomial if disagreements <50) | Multiple independent train/test splits |
| Small/medium (hundreds–thousands) | Repeated stratified 10-fold CV (`.632+` bootstrap for tiny n) | Nested CV (outer 5/10, inner 2/5) | Cochran's Q, then pairwise McNemar with Bonferroni | Alpaydin combined 5x2cv F (or Dietterich 5x2cv t) |

Forbidden regardless of size: proportion-difference z-test (inflated type-I error), resampled paired t-test, single-layer CV best-validation-score-as-generalization.

## Leakage audit table (Kapoor 8-category taxonomy)

| # | Category | Audit question |
|---|---|---|
| 1 | No independent test set | Is there a test set that never participated in any fit, and an OOD/external set for generalization claims? |
| 2 | Preprocessing leakage | Did any statistic (mean, imputation, PCA, feature selection, oversampling) get computed before the split or on all data? |
| 3 | Feature/target leakage | Is every feature available at prediction time — no future info, no proxy targets, no metadata artifacts? |
| 4 | Tuning/selection leakage | Was the test set used for early stopping, threshold choice, or model selection? (It must be touched once, at the end.) |
| 5 | Non-independent samples / group overlap | Do train and test share groups (patient, device, session, cluster)? Was dedup done before splitting? |
| 6 | Data-augmentation leakage | Was augmentation applied before the split, so originals and augmented copies straddle sets? |
| 7 | CV contamination | Is feature engineering redone inside each fold; GroupKFold/TimeSeriesSplit used where structure exists? |
| 8 | Engineering leakage | Are pipeline states reset per run, seeds fixed and shared across split/clean, no global statistics, test set physically isolated? |
| 9 | Shortcut / spurious correlation / protected attribute | Do models exploit site, camera, timing, or group artifacts instead of the intended signal (shortcut learning)? Are protected attributes (race, sex, age) encoded in the input in ways that change the claim? |
| 10 | Retrieval / system-level leakage (RAG, external data) | Does the retrieval corpus or any external data contain information from after the query/target time (future leakage into retrieval)? Does it overlap the evaluation queries' own sources? |

- **State the target distribution before code**: write down the real-world scenario the evaluation must represent, where the evaluation data comes from, and why it represents the scenario — **Why**: Dev/Test sets must come from the distribution you want to generalize to, not from whatever data is handy; mixing training-distribution data into evaluation silently narrows the claim, and the target-distribution statement is what makes that checkable (ML Yearning, ch. 1–12) — **How**: before any code, write a one-paragraph evaluation-distribution statement (scenario, data source, representativeness); evaluation sets contain no training-distribution impurities.

## Checklist
- [ ] Evaluation-distribution statement written before code (scenario, data source, why it represents the scenario)
- [ ] Split happens before any preprocessing; test set hashed, isolated, and scheduled for exactly one evaluation
- [ ] Hidden groups inventoried; group- or time-aware split used where any exist; dedup before split
- [ ] All statistics fit on training data only; preprocessing nested inside the CV pipeline
- [ ] Conclusion type declared (performance estimation / model selection / algorithm comparison) and the protocol cell from the decision card written into the design doc
- [ ] Baseline is a strong, tuned, simple method under the identical protocol; learning curves included
- [ ] Every added component has an ablation row (full / -A / -B / -A-B)
- [ ] Comparison pre-registered: hypothesis, primary metric, decision rule, dated before the first run
- [ ] Each planned claim's intended verification level is pre-declared (how it will be checked, before running)
- [ ] Leakage audit run against all 10 categories (incl. shortcut/protected-attribute and retrieval/system-level leakage) before trusting any number
- [ ] Data augmentation only inside the training set, after the split
- [ ] Data link recorded (raw data → metric: every processing node and order) — a model info sheet for the final report
- [ ] Data source provenance verified (collection protocol, licensing, no misattribution) — the chain starts before the raw data, not at it

## Anti-patterns
- **Fit-before-split**: scaler/PCA/imputation fit on all data → split first; statistics on train only.
- **Weak-baseline escort**: complex model vs default-parameter logistic regression → tuned strong baseline, identical protocol (Boulesteix).
- **Multi-axis change**: new model + new optimizer + new preprocessing, claimed as "our method" → one axis per comparison.
- **Test-set peeking**: tuning, early stopping, or threshold choice on the test set → the test set is a one-shot asset; after any tuning it has lost its generalization warrant (Raschka).
- **Random split on grouped data**: the same patient in train and test → group-aware split (Kapoor #5).
- **Wrong protocol for the claim**: McNemar on a fixed split presented as "algorithm X is better" → 5x2cv F (Raschka: model comparison ≠ algorithm comparison).

## Bad → good
- **bad**: "We propose an A+B+C fusion framework, +8 points over baseline." (no ablations, unknown which component works, baseline tuning unknown)
- **good**: "Full +8.0; −A +1.2; −B +7.1; −C +7.8 → the gain comes from A; B and C are not significant under the paired test and were dropped from the final protocol. Baseline: tuned logistic regression, identical splits and tuning budget."
- **bad**: Civil-war-style: complex ML vs logistic regression, default preprocessing on all data, random split, no leakage audit, "complex models win."
- **good**: Split first (group-aware), preprocessing fit on train only, leakage audit passes all 8 categories, complex model vs tuned LR under the identical protocol — and the report shows both learning curves so the reader can see the data regime where the gap (if any) holds.

## Relationships
- Hypotheses and the decisive experiment: `practices/question` + `principles/falsifiability`
- Statistics, variance, and tests for the chosen protocol cell: `practices/measure`
- Ablation discipline and strong baselines as parsimony: `principles/parsimony`
- The reproducibility record starts at design time (configs, seeds, split script): `principles/reproducibility` + `practices/reproduce`
- The leakage audit is re-run at interpretation time: `practices/verify`; data-link disclosure in reports: `practices/present`
- Test-set sealing is also the first honesty line: `principles/honesty`

## Sources
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../references/03-domingos.md` (L1 one-axis attribution; L2 generalization; L4 bias/variance)
- Kapoor & Narayanan, "Leakage and the Reproducibility Crisis in ML-based Science," Patterns 2023 — `../../references/05-kapoor.md` (8-category taxonomy, split-first defense, civil-war reproduction)
- Raschka, "Model Evaluation, Model Selection, and Algorithm Selection in ML," arXiv 1811.12808 — `../../references/04-raschka.md` (Fig. 23 decision card; McNemar / 5x2cv F; nested CV)
- Boulesteix, "A Plea for Neutral Comparison Studies in Computational Sciences," PLoS ONE, 2013
- Gelman & Loken, "The Garden of Forking Paths" (2013) — pre-registration rationale
- Varma & Simon (2006) nested CV; Kohavi (1995) stratified CV; Efron & Tibshirani (1997) .632+ bootstrap; Dietterich (1998) / Alpaydin (1999) 5x2cv tests — as cited in Raschka's survey
