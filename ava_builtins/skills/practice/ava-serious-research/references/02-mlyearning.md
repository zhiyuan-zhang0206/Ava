# 02 · Machine Learning Yearning (Andrew Ng)

**Source**: Andrew Ng, *Machine Learning Yearning*, DeepLearning.AI, 2018 (free online book, 58 chapters). Official page: <https://info.deeplearning.ai/machine-learning-yearning-book> · PDF: <https://home-wordpress.deeplearning.ai/wp-content/uploads/2022/03/andrew-ng-machine-learning-yearning.pdf>. A companion volume to Ng's Deep Learning Specialization; aimed at AI engineers and project teams rather than researchers, but its measurement-first discipline maps directly onto research practice.

---

## 0. One-sentence core

> ML projects rarely fail because the algorithm is not strong enough — they fail because the **goal was never pinned down, the data was split wrong, or the bottleneck was misdiagnosed**. Lock the target with a Dev/Test set and a single-number metric; diagnose the current bottleneck with the avoidable-bias / variance / data-mismatch triangle plus quantitative error analysis; then spend resources only on the one change with the largest ceiling for improvement. Measure first, act second.

---

## 1. Goals & evaluation: define "what success means" first (Ch 1–12)

### 1.1 Dev/Test sets: the anchor of evaluation

**Core content**: The Dev set and Test set must come from **strictly the same distribution**, and must represent the **real application scenario** where the system is expected to perform well in the future. The training set may contain off-distribution data (e.g. web-scraped images), but Dev/Test must never contain such impurities. In the big-data era the traditional 70%/30% split no longer applies: Dev/Test only need to be large enough for statistical decisions (e.g. detecting a 0.1% improvement), typically on the order of 1,000–10,000 examples, and can be far below 30% of the total data.

**Operational translation for ML research**:
- Step 1 of any project is an **evaluation-distribution statement**: what real scenario do we want the system to excel in, where does the evaluation data come from, and why does it represent that scenario? Not "use the standard benchmark" — declare the target distribution explicitly.
- Mixing other sources into the training set (web-scraped, synthetic, transferred) is fine, but the protocol must state that the evaluation sets contain no training-distribution impurities.
- Justify evaluation-set size in the experimental design: what is the smallest improvement we need to detect, and is 1,000–10,000 examples enough? (design module: protocol-first.)

### 1.2 Single-number metric + optimizing/satisficing metrics

**Core content**: Establish a **single-number evaluation metric** (e.g. F1 or a weighted average) — otherwise Precision and Recall trade off and team decisions stall. With $N$ evaluation dimensions (accuracy, latency, memory, …), set $N-1$ as **satisficing metrics** (meet a threshold, good enough) and keep 1 as the **optimizing metric** (minimize/maximize subject to the constraints).

**Operational translation**:
- Every experiment report must carry one **headline number**; with multiple dimensions, state explicitly which is the optimizing metric and which are satisficing (e.g. memory < threshold is satisficing, accuracy is the optimizing target).
- Ban the vague "we improved A but slightly regressed B" phrasing — the optimizing/satisficing frame makes the trade-off explicit. This is the origin of the measure module's metric-definition check.
- Multi-objective research (e.g. agent systems watching cost, latency and quality together) uses "1 optimizing + N−1 satisficing" to keep decisions decidable.

---

## 2. Rapid prototyping & quantitative error analysis (Ch 13–19)

### 2.1 An end-to-end baseline within days

**Core content**: Build a minimal working system **within days** at the start of a project; never build an over-complex model without data feedback. Run the initial system, analyze its concrete errors, and pick the improvement direction with the largest **ceiling** on accuracy.

**Operational translation**:
- The first step of research is not "design the perfect experiment" but **running the smallest end-to-end pipeline and getting the first real outputs** — the reproduce module's "first-run log" belongs on day 1, not after the protocol is finalized.
- Model/complexity investment is gated on data feedback: no first results, no complex feature engineering or architectures (design module: baseline-first discipline).

### 2.2 Error analysis: tabulate, count, compute the ceiling

**Core content**: Sample ~100 misclassified Dev examples, build a table and **count error types in parallel** (blur, specific class confusions, label errors, …). If an error type accounts for only 5% of misclassified examples, fixing it completely can reduce total error by at most 5% — do not invest heavily in it first. Fix label errors only when they seriously impair the ability to compare algorithms; if you change Dev labels, change Test labels the same way. Split Dev into an **Eyeball set** (for human inspection and error analysis; frequent contact overfits your judgment) and a **Blackbox set** (used only for automatic evaluation and hyperparameter tuning; never look at it).

**Operational translation**:
- On receiving experimental results, the first step is **error decomposition**: classify and count the failures, log them in the experiment notebook, compute each direction's ceiling, then decide the next step (verify module: failure analysis).
- Use ceiling arithmetic against "impression-driven direction choice": seeing 3 blurry samples and committing a month to blur-robustness is a named anti-pattern.
- **Blackbox discipline**: the evaluation set is physically isolated and touched only at final evaluation; all human inspection happens on the Eyeball subset. Stronger than "look at the test set once" — the tuning set itself is split into "may look" and "must not look".

---

## 3. Bias/variance diagnosis: quantify the bottleneck (Ch 20–32)

**Core content**:
$$\text{Total error} = \text{unavoidable bias (Bayes error)} + \text{avoidable bias} + \text{variance} + \text{data-mismatch error}$$
- **High avoidable bias** (training error far above Bayes error): increase model capacity (more layers/units), add features based on error analysis, reduce regularization, change architecture. **More training data does NOT fix high bias.**
- **High variance** (Dev error far above training error): add training data, add regularization (L1/L2, dropout), early stopping, feature selection.
- **Learning curves**: plot training and Dev error against training-set size. If both curves are flat and converge at high error, collecting more data is useless.

**Operational translation**:
- When results are bad, **diagnose underfitting vs overfitting before acting** — blindly adding data and blindly adding model capacity are both named resource wastes.
- Report error as a **decomposition** (avoidable bias vs variance vs data mismatch), not a single total (measure module: error-component breakdown).
- Learning curves are a standard reproduce-module artifact: training/validation error vs data size from one run, supporting the "is more data useful" conclusion.

---

## 4. Benchmarking against human-level performance (Ch 33–35)

**Core content**: On tasks humans are good at, expert performance (or a panel of experts) is a good **proxy for Bayes error**. Then avoidable bias ≈ human-level − training error, and variance ≈ training error − Dev error, telling you whether to work on fit or generalization next. **Once the algorithm surpasses human level**, estimating Bayes error and getting intuitive error analysis both become very hard, and iteration usually slows down.

**Operational translation**:
- Fix a **reference level** at project start — human experts / current SOTA / a simple baseline — as the anchor for avoidable bias. This is also a quantitative input to the question module's "is this problem worth doing": how far is the current system from the reference?
- After surpassing the reference, **slower iteration is the norm** — do not misread it as method failure.
- Report an avoidable-bias estimate (human level − training error) so readers can see how much headroom remains in principle.

---

## 5. Data mismatch: the Training-Dev set (Ch 36–43)

**Core content**: When the training distribution differs from the target distribution, hold out a slice of the **training distribution** as a Training-Dev set (not used for training) and diagnose with the error chain:
1. Training error → Training-Dev error: a large gap = **high variance**.
2. Training-Dev error → Dev error: a large gap = **data mismatch** (training and test distributions too far apart).

To fix mismatch: use error analysis to pin down the distribution difference, collect data matching the Dev distribution, or use artificial data synthesis. **Watch out**: severe overfitting to synthetic artifacts (a single background noise type, a limited set of 3D render models).

**Operational translation**:
- Train/eval distribution differences are the norm in modern ML (web/synthetic/transfer data vs real scenarios) — research must **state and diagnose** this explicitly instead of defaulting to "bad generalization = model problem".
- The Training-Dev error chain splits "bad generalization" into variance vs distribution shift — two separately addressable problems (design module: train/eval distribution consistency check).
- Any use of synthetic data (augmentation, simulation, LLM-generated data) must report a **check for overfitting to synthetic artifacts** — especially relevant for current LLM-synthesized-data research.

---

## 6. Optimization Verification Test: search error vs scoring-function error (Ch 44–46)

**Core content**: When the system pairs a **scoring/reward function** with an **approximate search algorithm** (beam search, RL policy) and the output is wrong, compare the scores of the correct reference output $S^*$ and the model output $S_{out}$:
- If $Score(S^*) > Score(S_{out})$: the scoring function is fine — the **search algorithm** failed to find the optimum (widen beam / improve search).
- If $Score(S^*) \le Score(S_{out})$: the search found a higher-scoring item — the **scoring/reward function** is misdefined (redesign the loss or the reward matrix $R$).

**Operational translation**:
- For inference/generation research (LLM decoding, RL, search-augmented methods), **run the optimization verification test to attribute the error before changing anything** — "search didn't find it" and "the objective rewards the wrong behavior" need completely different fixes.
- verify module: attribution before repair — the attribution must carry test evidence (the $S^*$ vs $S_{out}$ score comparison), never impression.

---

## 7. End-to-end vs pipeline + error attribution by parts (Ch 47–58)

**Core content**:
- **End-to-end**: fewer hand-engineered assumptions, directly fits the complex input→output mapping, high ceiling; but needs massive $(x,y)$ paired data.
- **Pipeline**: small data needs; decomposes a complex task into simple subtasks; can exploit large public datasets per intermediate step; but accumulates errors and carries hand-designed assumptions.
- **Choice criteria**: ① is there abundant labeled data for the intermediate nodes (e.g. license-plate recognition split into "locate plate" + "read characters" is easier to get data for than one shot)? ② are the subtasks simple enough?
- **Ground-Truth replacement**: in a multi-stage pipeline, replace each upstream module's output with human-perfect labels one at a time and feed downstream: if the whole system becomes correct, the error is attributed to that upstream module; if still wrong, keep attributing downstream.
- If every module independently reaches human level but overall performance is still poor → the **pipeline architecture itself is flawed** (upstream drops information the downstream decision needs — e.g. autonomous driving omitting lane-line detection).

**Operational translation**:
- Architecture decisions (end-to-end vs staged) must be argued from **data availability**, not fashion (design module: architecture-choice check).
- For multi-module systems (e.g. agent systems: retrieval → planning → generation), attribute errors with the **replacement method** — the reproduce/verify module's component-level attribution check: swap each stage's real input for an ideal one and see which swap fixes the whole.
- **"Every component is good but the whole is bad" must trigger an architecture review**: for agents, the classic case is the retrieval layer not passing evidence to the generation layer — component metrics cannot rescue that. Run both component-level and whole-system evaluation.

---

## 8. Anti-patterns: the book's "do not" list

1. **Blind 70%/30% random splits** — with heterogeneous data sources, a global random split pollutes Dev/Test with unrealistic data.
2. **Dev/Test distribution mismatch** — iterating on Dev for months, then discovering at release that Test is a different distribution, voiding all optimization.
3. **Premature optimization** — months spent designing "perfect" feature engineering or complex networks before a prototype and error analysis exist.
4. **Blindly collecting more data when biased** — under high avoidable bias, more data neither helps nor pays for itself.
5. **Error analysis by impression** — committing a month to an error type seen in a few examples without counting its share of Dev mistakes.
6. **Polluting Blackbox/Test sets** — frequent eyeballing or tuning against Test destroys evaluation objectivity.
7. **End-to-end on small data** — abandoning a sensible pipeline without massive $(x,y)$ pairs; generalization collapses.

**Operational translation**: these seven convert directly into negative checks for the research workflow — #5 → verify module's "error analysis must be quantitative"; #6 → the "evaluation set touched once" discipline; #4 → "diagnose bias/variance before adding data"; #7 → "end-to-end vs pipeline needs a data-availability argument".

---

## Contribution map to ava-serious-research

| Module | Contribution |
|--------|--------------|
| question | Reference level (human/SOTA/baseline) as a quantitative anchor for "is this worth doing"; every research question carries an evaluation-distribution statement (on what scenario it is answered) |
| literature | The book itself does not cover literature methods; but its "training distribution may be mixed, evaluation distribution must be pure" discipline transfers to judging dataset choices in the literature (inferred — the source extract does not discuss this) |
| design | Baseline-before-perfect-design; end-to-end vs pipeline argued from data availability; error analysis precedes improvement design |
| measure | Single-number metric + optimizing/satisficing frame; evaluation-distribution statement; error decomposition (avoidable bias / variance / data mismatch); ceiling arithmetic |
| reproduce | Learning curves as a standard artifact; Training-Dev set protocol; Eyeball/Blackbox physical separation keeps evaluation objective and reproducible |
| present | Reports carry a headline number; trade-offs made explicit via optimizing/satisficing; error reported as components, not a single total |
| verify | Optimization Verification Test for error attribution; Ground-Truth replacement for component-level attribution; the seven anti-patterns as a self-check list |

## Candidate checklist items

- [ ] MUST the research question carries an evaluation-distribution statement: what real scenario Dev/Test represent, and whether training data mixes other distributions
- [ ] MUST every experiment has one headline metric; with multiple dimensions, exactly 1 optimizing and N−1 satisficing metrics are named
- [ ] MUST first experimental results come with an error decomposition: failure-sample counts by category + per-direction improvement ceiling
- [ ] MUST performance-bottleneck diagnosis distinguishes avoidable bias / variance / data mismatch, with numeric evidence (training error, Dev error, reference level)
- [ ] MUST evaluation sets obey Blackbox discipline: used only at final evaluation; human inspection confined to the Eyeball subset
- [ ] MUST search/RL errors are attributed with an Optimization Verification Test before any fix (search vs scoring function)
- [ ] MUST multi-module pipelines are attributed component-wise with Ground-Truth replacement; if components are good but the whole is bad, the architecture is re-examined
- [ ] SHOULD a learning curve (training/validation error vs data size) supports any "more data is useful" conclusion
- [ ] SHOULD a reference level (human/SOTA/baseline) and an avoidable-bias estimate are reported
- [ ] SHOULD train/eval distribution mismatch is diagnosed with the Training-Dev error chain and reported
- [ ] SHOULD synthetic-data use includes an overfitting-to-synthetic-artifacts risk check
- [ ] SHOULD end-to-end vs pipeline choices come with a data-availability argument
