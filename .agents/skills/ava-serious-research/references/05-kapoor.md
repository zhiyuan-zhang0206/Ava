# 05 · Leakage and the Reproducibility Crisis in ML-based Science

**Authors**: Sayash Kapoor & Arvind Narayanan (Princeton University — Dept. of Computer Science & Center for Information Technology Policy)
**Status**: Reference entry for the ava-serious-research skill (built from the extracted source; facts verified against the paper's abstract).
**Review status (Chinese edition)**: ⏳ pending user review (2026-08-06).

---

## 0. One-line core

> Much of the "remarkably high accuracy" in ML-based science is an artifact of **data leakage** — test information reaching the model during training. The fix is a discipline established at the start of experimental design, not a post-hoc check: **split first, then process; seal the test set until the final evaluation.**

---

## 1. Source & citation

- **Paper**: Kapoor, S., & Narayanan, A. (2023). *Leakage and the reproducibility crisis in machine-learning-based science*. Patterns, 4(9), 100804. https://doi.org/10.1016/j.patter.2023.100804 (published 2023-08-04; eCollection 2023-09-08)
- **arXiv preprint**: *Leakage and the Reproducibility Crisis in ML-based Science*, arXiv:2207.07048 (2022). https://arxiv.org/abs/2207.07048
- **Project page**: https://reproducible.cs.princeton.edu/ (Princeton Reproducibility Project)
- **Type**: meta-research paper (survey + taxonomy + reproducibility study), not a book.

### 1.1 The evidence (faithful to the paper's abstract)

Through a survey of the literature in research communities that adopted ML, the authors found **17 fields** (medicine, climate science, social science, materials science, etc.) where leakage errors have been found, **collectively affecting 329 papers**, in some cases leading to "wildly overoptimistic" conclusions.

They additionally ran a reproducibility study in **civil war prediction** — a field where complex ML models were believed to vastly outperform logistic regression. **Every paper claiming superior performance of complex ML models failed to reproduce due to data leakage**, and complex models did not perform substantively better than decades-old logistic regression. None of these errors could have been caught by reading the papers alone; the authors argue their proposed **model info sheets** would have enabled detection in every case.

### 1.2 Why leakage is widespread: black-box use + misaligned incentives

- **Black-box use**: domain scientists treat ML as a tool and blindly follow library defaults — the most common error is calling `fit_transform` on the full dataset (scaling, imputation, feature selection) *before* `train_test_split`.
- **Incentive misalignment**: review processes reward high (SOTA) metrics and lack code review for leakage; papers with inflated metrics are *more* likely to be accepted at top venues — a vicious cycle.

---

## 2. The taxonomy: 8 types of leakage (30+ concrete patterns)

The survey yields a fine-grained taxonomy of **8 types of leakage, ranging from textbook errors to open research problems**:

| # | Type | Representative patterns | Operational translation for agent research |
|---|------|------------------------|---------------------------------------------|
| 1 | **No independent test set** | No test set; complete overlap with training; lack of OOD/external validation | Every experiment needs a test set that never touched any fit; "generalization" claims need external-source validation or the claim degrades to "valid within the collection distribution" |
| 2 | **Pre-processing leakage** | Scaling/normalization, feature selection, imputation, dimensionality reduction, oversampling, discretization before split | The **first code line after loading is the split**; every statistic (mean, median, quantile, PCA loadings) is computed on training data only |
| 3 | **Feature / target leakage** | Direct target leakage; indirect proxy variables; temporal/future leakage; spatial/geographic leakage; metadata & spurious artifacts | Ask of every feature: "is this truly available at prediction time?"; time series split by time; watch for acquisition artifacts (scanner borders, file headers) |
| 4 | **Model tuning / selection leakage** | Hyperparameter tuning, early stopping, model selection, threshold tuning on the test set | The test set is touched **exactly once** (final evaluation); all tuning uses the validation set; record which set produced every number |
| 5 | **Non-independent samples / group overlap** | Subject/patient overlap; kinship; same device/session; patch-level splits; unidentified duplicates | Before splitting ask "is there a hidden group structure?" (Patient ID / Cluster ID / timestamp); use group-aware splits; deduplicate first |
| 6 | **Data augmentation leakage** | Augmentation applied before split, so originals and augmented copies land in different sets | Augmentation only inside the **training** set after the split |
| 7 | **Cross-validation contamination** | Feature engineering outside the folds; random K-Fold ignoring group structure; random K-Fold on time series | Feature engineering redone per fold; GroupKFold / TimeSeriesSplit where structure demands |
| 8 | **Engineering / implementation leakage** | Pipeline state not reset; shared global statistics; mismatched random seeds; pseudo-label/semi-supervised contamination; data filtering before split; target encoding; improper metric computation | Reset state per run; fixed, consistent seeds; physically isolate the test set from every training path |

> Note: the 8 types span from *textbook errors* (1–2) to *open research problems* (some subtypes of 3 and 5). Missing test sets are amateur errors; group-structure and spurious-correlation leakage are usually the deepest-hidden ones.

---

## 3. The leakage-prevention framework (three layers)

1. **Strict early splitting**: the first step of any preprocessing pipeline is partitioning into Train / Val / Test; the test set is **sealed** — it must not participate in *any* statistic (means, imputation, feature selection) until the final evaluation.
2. **Group- and time-aware splitting protocol**: identify dependency structure (Patient ID, Cluster ID, timestamp) before splitting; group data **must** use group-based splits, time series **must** use temporal splits — never the random-split default.
3. **Model info sheets**: borrowing from hardware datasheets / Model Cards, authors document the full **data pipeline** — every processing step from raw data to evaluation metric, in order. The authors argue this would catch every leakage type in their survey (in the civil-war case study, no leakage was detectable by reading the papers, but model info sheets would have flagged each one).

**Operational translation**: hard-code the pipeline order — load → deduplicate (training-side statistics only) → split → preprocess inside training → model/tune on validation → evaluate test set once → record the data lineage. Experiment logs must carry a "data lineage" field (inputs/outputs of each step, split seed, processing order).

---

## 4. Main arguments & recommendations

**Arguments**:
1. Data leakage is a leading cause of the reproducibility crisis in ML-based science — inflated accuracies collapse on deployment or independent test sets.
2. Leakage is pervasive and underappreciated because of black-box ML use.
3. Academic incentives are misaligned — review rewards high metrics, lacks code review, so leaky papers are more likely to be published.

**Recommendations**:
- **Researchers**: use encapsulated isolation pipelines (e.g., scikit-learn `Pipeline`); abandon the random-split default; scrutinize hidden group associations in the data.
- **Reviewers & journals**: make "sound split logic + code" a hard review criterion; require a leakage checklist and runnable code.
- **Software & open-source community**: redesign ML library APIs so unsafe operations (preprocessing on the full dataset) are harder to perform and safe pipelines are the default; build automated static/dynamic tools that detect `fit()` calls before splitting.

---

## 5. Contribution mapping to ava-serious-research

| Module | Contribution |
|--------|--------------|
| question | External validity enters the question boundary: every claim carries "on which data distribution does this hold"; missing OOD validation = overclaim |
| literature | Use the leakage taxonomy to spot inflated numbers in prior work — a source of contradictions/gaps; a lens for judging whether existing SOTA claims are trustworthy |
| design | The first design decision is the **splitting protocol** (early split + group/time awareness); test-set sealing; "complex vs simple baseline" comparisons must be leakage-free |
| measure | Metrics computed once on the isolated test set; inflated metrics trigger a leakage audit before believing the conclusion — leakage is the strongest counter-example to a warrant |
| reproduce | Leakage-freedom is a precondition of reproducibility: pipeline code, fit/transform isolation, fixed seeds, state reset, deduplication; lineage recorded as experiments run |
| present | Model info sheets = disclose the data lineage in the paper; readers can independently re-audit every step from raw data to metric |
| verify | The taxonomy + checklist are the hard gate for self-review and adversarial review: "rule out leakage before accepting conclusions" |

---

## 6. Candidate checklist items

**MUST (leakage floor)**
- [ ] Data split happens before *any* preprocessing (scaling / imputation / feature selection / dimensionality reduction / augmentation / outlier filtering)
- [ ] Group or temporal structure present → group-aware or temporal split used; test set contains no group IDs seen in training
- [ ] Feature selection, PCA, imputation fitted on training only; test set only passes through `transform`
- [ ] Hyperparameters / early stopping / model selection / thresholds tuned on validation only; test set evaluated exactly once
- [ ] Deduplication performed before splitting; no near-duplicate samples across subsets
- [ ] Data augmentation applied only inside the training set
- [ ] fit/transform-isolating pipeline used; no global-state or global-statistics leakage; random seeds fixed and consistent across cleaning and splitting

**SHOULD (rigor)**
- [ ] OOD / external validation results provided when claiming generalization
- [ ] Model info sheet / data-lineage statement included (each processing step from raw data to metric, in order)
- [ ] Cross-validation respects group structure (GroupKFold / TimeSeriesSplit); feature engineering redone inside folds
- [ ] Code published reproducibly, including split logic, seeds, and run order
