# 04 · Model Evaluation, Model Selection, and Algorithm Selection in Machine Learning

**Author**: Sebastian Raschka (Dept. of Statistics, University of Wisconsin–Madison; author of Python-machine-learning books and the MLxtend library)
**Nature**: Systematic methodology survey paper — arXiv:1811.12808, first posted November 2018, v3 revised November 11, 2020. Not a book: an extensive review that consolidates evaluation protocols, resampling methods, and statistical tests scattered across textbooks and papers, ending in a decision tree (Fig. 23) keyed on dataset size. Its single question: **"Why do you claim model A is better than model B?"**
**Audit status**: ⏳ pending user review (2026-08-06; parity target: 05-kapoor.md)

---

## 0. One-sentence core

> Evaluation is not a "run the accuracy" step but a **goal-driven protocol-selection problem**: first decide whether you are answering performance estimation, model selection, or algorithm comparison; then pick the protocol (holdout / k-fold / nested CV / bootstrap); then pick the statistical test (McNemar / 5x2cv F). Every layer of mismatch pushes conclusions systematically optimistic — and **errors in the evaluation method are harder to spot than errors in the model itself**.

---

## 1. Evaluation protocols: three goals, three protocols

### 1.1 The three goals: declare your conclusion type first

The paper opens by separating experimental evaluation into three goals that are often conflated but require entirely different protocols:

| Goal | Question being answered | Carrier of the conclusion |
|------|------------------------|---------------------------|
| Performance Estimation | How well does this (already fixed) model generalize to unseen data? | Point estimate + uncertainty (variance / CI) |
| Model Selection | Which hyperparameter setting is best in a given hypothesis space? | The chosen hyperparameters (not the final score!) |
| Algorithm Comparison | Which learning algorithm is on average better on this problem domain? | Test statistic + p-value |

**Operational translation for ML research**: the first step of the design phase is to write a one-line "conclusion-type declaration". The three types bind to three protocols: performance estimation → holdout or repeated k-fold; model selection → 3-way holdout or nested CV; algorithm comparison → 5x2cv test. The most common mistake in research reports: claiming "our method beats the baseline" (an algorithm-comparison claim) while only reporting the metric difference of two fixed model instances on one fixed test set — the classic conflation of §3.5 below.

### 1.2 Holdout: stratification, pessimistic bias, 2-way vs 3-way

- **Stratified sampling is mandatory**: simple random subsampling changes the statistical properties of the subsets (means, proportions, variances); on small or class-imbalanced datasets it can skew class ratios badly or even drop rare classes from the test set entirely. Kohavi (1995) gives empirical evidence that stratification reduces both the bias and the variance of the estimate.
- **Pessimistic bias**: holding out a test set shrinks the training data; if the model is not at its capacity limit, the estimate systematically *underestimates* the performance of a model trained on the full dataset.
- **2-way vs 3-way**: 2-way (Train/Test) is only for evaluating a single model whose hyperparameters are already fixed; whenever hyperparameter tuning exists, 3-way (Train/Validation/Test) is required to prevent test-set information leakage.

**Operational translation**: code discipline — every `train_test_split` / `KFold` on a classification task must set `stratify`; reporting discipline — if the conclusion concerns deployment performance, retrain the final model on the full data after evaluation and say so in the report (otherwise you report a pessimistically biased number); semantic discipline — once any hyperparameter has been tuned (even one round), the test set has lost its right to evaluate generalization. **The test set is a single-use asset**, for the final verdict only.

### 1.3 k-fold cross-validation: the bias–variance trade-off

- Split the data into k non-overlapping folds; each fold serves once as the validation set while the remaining k−1 folds train; average the k performances.
- **Choosing k is a bias–variance trade-off**: larger k (e.g. LOOCV, k=n) makes training sets closer to the full data → lower pessimistic bias; but training sets overlap almost completely (n−1 shared samples), models become highly correlated → **the variance of the estimate grows** (Hastie et al., 2009).
- Empirical recommendation: **stratified k=10 (or k=5) CV** offers the best bias–variance balance (Kohavi, 1995); **repeated k-fold CV** (averaging over several different splits) further improves estimate precision.

**Operational translation**: for small/medium datasets (hundreds to a few thousand samples) the default protocol is repeated stratified 10-fold CV. Report mean ± std (across folds/repeats), not a bare number — a single point estimate supports no "we are better" claim. Beware the intuition that LOOCV is "the most rigorous": it has the smallest bias but the largest variance, is sensitive to discontinuous losses, and is expensive (§3.4).

### 1.4 Nested cross-validation: total isolation of tuning from evaluation

- **Structure**: the outer loop (e.g. 5 folds) estimates generalization error; the inner loop (e.g. 2/5 folds) does hyperparameter tuning. Within each outer fold: search hyperparameters on the inner training folds → retrain with the best setting on the outer training fold → evaluate on the outer test fold.
- **Why necessary**: in single-level CV, "pick hyperparameters on the validation folds and report the best validation score as the generalization score" burns the test set once — hyperparameter optimization is itself a training process and introduces optimistic bias. Varma & Simon (2006) show empirically that nested CV substantially reduces this bias.
- **Applicable**: small samples + hyperparameter tuning.

**Operational translation**: the tuning pipeline for small-sample experiments (benchmarks with a few hundred samples, scarce-domain data) is a nested loop: `for outer_fold: GridSearchCV(inner) → retrain best hyperparameters on outer_train → evaluate on outer_test` (in scikit-learn, `cross_val_score(estimator=GridSearchCV(...))` is exactly nested). Reports must state that "the generalization performance comes from the outer folds", otherwise the reader cannot tell whether optimistic bias crept in.

### 1.5 Bootstrap: uncertainty estimation for very small samples

- **LOOB (Leave-One-Out Bootstrap)**: draw n samples with replacement as the training set; the probability a given sample is never drawn is (1−1/n)ⁿ → 1/e ≈ 36.8%. Each bootstrap round thus contains ~63.2% unique samples, with the remaining ~36.8% as the out-of-bag (OOB) test set.
- **.632 Bootstrap** (Efron, 1983): bootstrap training sets hold only 63.2% of the data → strong pessimistic bias; using resubstitution accuracy (self-assessment on the training set) → extreme optimistic bias. The .632 estimator blends both with a fixed weight: ACC_boot = (1/b) Σ (0.632·ACC_oob + 0.368·ACC_resub).
- **.632+ Bootstrap** (Efron & Tibshirani, 1997): when the model overfits heavily, .632 remains optimistically biased; .632+ introduces the relative overfitting rate R and the no-information rate γ to adjust the weight dynamically: ω = 0.632/(1 − 0.368·R).

**Operational translation**: with very small samples (tens of examples), holdout and k-fold are both unstable; use .632+ bootstrap for performance and confidence intervals (implemented in MLxtend's `bootstrap_point632_score`). Note that bootstrap rounds overlap → estimates are not independent → **never use them for conventional significance tests** (same limitation as Monte Carlo CV).

---

## 2. Statistical comparison: model comparison ≠ algorithm comparison

### 2.1 Model comparison (fixed model instances on one test set)

> Goal: compare the predictive performance of **specific model instances trained on the same training set** and evaluated on **the same test set**.

- **Difference-of-proportions z-test — not recommended**: evaluating two models on the same test set violates the independence assumption outright; the type-I error rate (false positives) is extremely high (Dietterich, 1998).
- **McNemar's test — recommended**: considers only the samples on which the two models **disagree** (cells B and C of the 2×2 contingency table); the continuity-corrected statistic χ² = (|B−C|−1)²/(B+C) follows a χ² distribution with df=1; the approximation is accurate when B+C ≥ 50.
- **Exact binomial test**: when B+C < 50 (too few discordant samples) the χ² approximation breaks down; use the exact binomial test for a two-tailed p-value.
- **Multiple models (M ≥ 3)**: run an omnibus test first — Cochran's Q (a non-parametric generalization of McNemar, df=M−1) or the F-test of Looney (1988); if significant, follow with pairwise McNemar tests **with Bonferroni correction** (α_adj = α/m, m = number of comparisons) to control the family-wise error rate.

**Operational translation**: any "model A beats model B on this test set" claim must carry a McNemar p-value (or exact binomial), and report B+C to demonstrate the test's validity. Multi-model comparisons go omnibus-first, then corrected pairwise — this is exactly the "multiple-testing correction" checklist item of the verify module.

### 2.2 Algorithm comparison (average behavior over data fluctuation)

> Goal: assess the **learning algorithm itself** — its average generalization and stability under random fluctuations of the training data.

- **Resampled / k-fold paired t-test — not recommended**: training sets overlap heavily across rounds; the differences are neither independent nor normal; type-I error rate is extremely high.
- **Dietterich's 5x2cv paired t-test — recommended**: run 5 repetitions of 2-fold CV (50/50 splits), so training-set overlap stays low; the numerator uses only the first round's ACC_A,1, further reducing dependence; the statistic follows a t-distribution with df=5.
- **Alpaydin's combined 5x2cv F-test — most recommended**: the numerator uses all 10 split differences, making it more robust and more powerful; the statistic follows F(10,5).

**Operational translation**: any research-grade "our method vs baseline" claim (a conclusion that must hold across the randomness of data splits) uses Alpaydin's combined 5x2cv F-test; report "Combined 5x2cv F-test, F(10,5)=..., p=...". Never use the resampled paired t-test. (MLxtend ships ready-made implementations of McNemar, the 5x2cv t/F tests, and .632+.)

### 2.3 Wording discipline for the two claim types

- "**Model A is significantly better than model B on this test set**" — supported by McNemar; the conclusion is scoped to these two model instances and this one test set.
- "**Algorithm A is better than algorithm B on this problem**" — supported by the 5x2cv F-test; the conclusion spans the randomness of data partitioning.
- The two claims differ in strength and scope; using a McNemar result to claim "the algorithm is better" is overclaiming — the direct source of the present module's "conclusion strength must not exceed what the test supports" checklist item.

---

## 3. Five pitfalls (anti-patterns of evaluation methodology)

1. **Preprocessing / feature-selection leakage**: normalizing, imputing, or selecting features on the *entire dataset before splitting* leaks validation/test information into training → optimistic estimates.
   Fix: preprocessing must be nested strictly inside the CV loop — fit only on the current fold's training set, then apply to the validation fold (scikit-learn `Pipeline` enforces this by construction).
2. **Hyperparameter-tuning leakage (burning the test set)**: using the same test set both to search hyperparameters and to report final generalization accuracy → overly optimistic reported performance.
   Fix: 3-way holdout or nested CV; the test set is used exactly once.
3. **Type-I error inflation under multiple testing**: M=5 algorithms yield C(5,2)=10 pairwise comparisons; testing all at α=0.05 gives P(at least one false positive) = 1−0.95¹⁰ ≈ 40.1%.
   Fix: omnibus test first (Cochran's Q), then pairwise comparisons with Bonferroni correction.
4. **The LOOCV misconception**: "smallest bias = best" is wrong — the training sets of the folds overlap by (n−2)/(n−1), models are highly correlated, and the averaged estimate has variance substantially higher than 10-fold CV; it is also violently sensitive to discontinuous losses such as 0-1 loss.
5. **Confounding model comparison with algorithm comparison**: a significant McNemar on one fixed training set says nothing about the algorithms — sampling fluctuation of the training data is not accounted for. Algorithmic conclusions require the 5x2cv protocol.

**Operational translation**: these five are, verbatim, an adversarial self-review checklist for the verify module — when auditing your own (or someone else's) experiment code, go through each: where does the scaler's `fit` happen? How many times is the test set touched? How many comparisons were made, and were they corrected? Grep for `fit(` and test-set usage before drawing any conclusion.

---

## 4. The decision tree (Fig. 23): pick the protocol by dataset size

| Scenario / data regime | Goal | Recommended protocol / test | Disallowed / not recommended |
|---|---|---|---|
| **Large datasets** (tens of thousands to millions, e.g. deep learning) | Performance estimation | 2-way holdout + normal-approximation CI | LOOCV (computational cost) |
| | Model selection | 3-way holdout (Train/Val/Test) | Tuning hyperparameters on Test while reporting metrics |
| | Model comparison | McNemar (exact binomial if discordant pairs < 50) | Difference-of-proportions z-test (huge type-I error) |
| | Algorithm comparison | Multiple independent train/test splits | Resampled paired t-test |
| **Small/medium datasets** (hundreds to thousands) | Performance estimation | Repeated stratified 10-fold CV (LOOCV or .632+ bootstrap for very small data) | Unstratified random splits |
| | Model selection + estimation | Nested CV (outer 5/10, inner 2/5 folds) | Single-level k-fold best validation score as the generalization score |
| | Multi-model comparison | Cochran's Q + post-hoc McNemar (Bonferroni) | Multiple uncorrected McNemar tests |
| | Algorithm comparison | Alpaydin combined 5x2cv F-test (alternative: Dietterich 5x2cv t) | k-fold paired t-test |

**Operational translation**: this table can be the design module's "protocol selection" decision card: for any new experiment, ask sample-size regime (large / small) → ask goal (performance estimation / model selection / model comparison / algorithm comparison) → look up protocol and test. Only 8 combinations in total.

---

## 5. Contribution mapping to ava-serious-research

| Module | Contribution |
|--------|--------------|
| question | The three-goal taxonomy: a research question must first declare its conclusion type (performance estimation / model selection / algorithm comparison); the type determines the protocol–test combination |
| literature | When citing others' SOTA / benchmark numbers, trace their evaluation protocol (split scheme, repetitions, test used) — numbers from non-equivalent protocols are not directly comparable |
| design | The Fig. 23 decision tree is an experiment-design template: sample size → protocol; small-sample tuning scenarios require nested CV; settle the protocol before writing code |
| measure | Point-estimate/inference pairing discipline: metrics give point estimates, tests give significance; making the warrant explicit = checking that the test's independence/pairing assumptions match the data-generation process |
| reproduce | Stratified splits, fixed random seeds, preprocessing inside Pipelines, recording splits/repetitions/seeds — the evaluation protocol itself must be reproducible |
| present | The methods section must state protocol and test; conclusion strength must not exceed what the test supports ("significantly better" vs "looks better") |
| verify | The five pitfalls are an anti-pattern list; the paper's five code-review checkpoints (stratification, pipeline sealing, single-use test set, multiple-testing correction, 5x2cv statistic) feed directly into adversarial self-review |

---

## 6. Candidate checklist

- [ ] MUST The experiment design first declares the conclusion type (performance estimation / model selection / algorithm comparison) and the protocol matches it (§1.1)
- [ ] MUST Data splits for classification use stratified sampling (`stratify` / `StratifiedKFold`)
- [ ] MUST Preprocessing and feature selection are nested inside the CV loop (fit inside a Pipeline; no global fit)
- [ ] MUST The test set is used exactly once, after all tuning/feature selection is finished (3-way holdout or nested CV)
- [ ] MUST Single-test-set model comparison uses McNemar (exact binomial when B+C<50); the difference-of-proportions z-test is forbidden
- [ ] MUST Comparing ≥3 models starts with an omnibus test (Cochran's Q / Looney F); post-hoc pairwise comparisons apply Bonferroni correction
- [ ] MUST Algorithm comparison uses the 5x2cv protocol (Alpaydin F or Dietterich t); the resampled paired t-test is forbidden
- [ ] MUST Small-sample tuning + performance estimation uses nested CV; "single-level CV best validation score as generalization score" is forbidden
- [ ] SHOULD Performance estimates report uncertainty (repeated-CV variance or confidence intervals)
- [ ] SHOULD After evaluation, the final deployed model is retrained on the full dataset (mitigating pessimistic bias)
- [ ] SHOULD Reproducibility notes state: split scheme, number of repetitions, random seeds, test and correction method

---

## Sources

- Raschka, S. (2018, v3 2020). *Model Evaluation, Model Selection, and Algorithm Selection in Machine Learning*. arXiv:1811.12808. https://arxiv.org/abs/1811.12808
- Kohavi, R. (1995). *A Study of Cross-Validation and Bootstrap for Accuracy Estimation and Model Selection*. IJCAI-95. https://dl.acm.org/doi/10.5555/1643031.1643047
- Dietterich, T. G. (1998). *Approximate Statistical Tests for Comparing Supervised Classification Learning Algorithms*. Neural Computation 10(7). https://dl.acm.org/doi/10.1162/089976698300017197
- Alpaydin, E. (1999). *Combined 5×2 cv F Test for Comparing Supervised Classification Learning Algorithms*. Neural Computation 11(8):1885–1892. https://direct.mit.edu/neco/article-abstract/11/8/1885/6310/
- Looney, S. W. (1988). *A Statistical Technique for Comparing the Accuracies of Several Classifiers*. Pattern Recognition Letters 8(2):87–94. https://doi.org/10.1016/0167-8655(88)90016-5
- Varma, S., & Simon, R. (2006). *Bias in Error Estimation When Using Cross-Validation for Model Selection*. BMC Bioinformatics 7:91. https://doi.org/10.1186/1471-2105-7-91
- Efron, B. (1983). *Estimating the Error Rate of a Prediction Rule: Improvement on Cross-Validation*. Journal of the American Statistical Association 78(382):316–331. https://doi.org/10.1080/01621459.1983.10477973
- Efron, B., & Tibshirani, R. (1997). *Improvements on Cross-Validation: The .632+ Bootstrap Method*. Journal of the American Statistical Association 92(438):548–560. https://sites.stat.washington.edu/courses/stat527/s13/readings/EfronTibshirani_JASA_1997.pdf
- Hastie, T., Tibshirani, R., & Friedman, J. (2009). *The Elements of Statistical Learning* (2nd ed.). Springer. https://hastie.su.domains/ElemStatLearn/
