# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../../references/03-domingos.md` (L1 one-axis attribution; L2 generalization; L4 bias/variance)
- Kapoor & Narayanan, "Leakage and the Reproducibility Crisis in ML-based Science," Patterns 2023 — `../../../references/05-kapoor.md` (8-category taxonomy, split-first defense, civil-war reproduction)
- Raschka, "Model Evaluation, Model Selection, and Algorithm Selection in ML," arXiv 1811.12808 — `../../../references/04-raschka.md` (Fig. 23 decision card; McNemar / 5x2cv F; nested CV)
- Boulesteix, "A Plea for Neutral Comparison Studies in Computational Sciences," PLoS ONE, 2013
- Gelman & Loken, "The Garden of Forking Paths" (2013) — pre-registration rationale
- Varma & Simon (2006) nested CV; Kohavi (1995) stratified CV; Efron & Tibshirani (1997) .632+ bootstrap; Dietterich (1998) / Alpaydin (1999) 5x2cv tests — as cited in Raschka's survey
