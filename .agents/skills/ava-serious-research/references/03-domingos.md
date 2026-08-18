# 03 · A Few Useful Things to Know About Machine Learning

**Source**: Pedro Domingos (University of Washington). This is a *paper*, not a book — the famous "12 lessons" paper, the most-cited practical-methodology piece in ML.

- Pedro Domingos, "A Few Useful Things to Know About Machine Learning," *Communications of the ACM*, 55(10): 78–87, October 2012.
- ACM Digital Library (DOI): https://doi.org/10.1145/2347736.2347755
- Author's freely available copy: http://homes.cs.washington.edu/~pedrod/papers/cacm12.pdf

**Status**: English reference version of the Chinese review (03-domingos.md, book-reviews-cn/), written for the ava-serious-research skill references. User review pending (2026-08-06). Content faithful to the source extract prepared from the paper.

---

## 0. One-line core

> Every machine learning algorithm is **Representation + Evaluation + Optimization**; success comes less from a cleverer algorithm than from discipline about **generalization, data, features, and causality** — most failures are polluted evaluation, ignored priors, wasted features, or causal overreach, not insufficient algorithmic cleverness.

---

## 1. The twelve lessons (verbatim titles)

1. Learning = Representation + Evaluation + Optimization
2. It's Generalization that Counts
3. Data Alone Is Not Enough
4. Overfitting Has Many Faces
5. Intuition Fails in High Dimensions
6. Theoretical Guarantees Are Not What They Seem
7. Feature Engineering Is the Key
8. More Data Beats a Cleverer Algorithm
9. Learn Many Models, Not Just One
10. Simplicity Does Not Imply Accuracy
11. Representable Does Not Imply Learnable
12. Correlation Does Not Imply Causation

---

## 2. The anatomy of learning: Representation + Evaluation + Optimization (L1)

**Core**: Every ML algorithm decomposes into three components — **representation** (the hypothesis space: which functions the model can express), **evaluation** (the objective function: how good models are distinguished from bad ones), and **optimization** (the search procedure that finds the highest-scoring model in the hypothesis space). Textbooks organize by representation, making evaluation and optimization look secondary; in fact, with the same hypothesis space, a different optimizer or evaluation function yields a completely different classifier.

**Trap**: The three components are not interchangeable levers — "a stronger model" and "a better optimizer" are different changes, yet both improve results and can impersonate each other.

**Operational translation for ML research**: The first step of experiment design is to state the three components explicitly: what is the hypothesis space, what is the evaluation function, what is the optimization procedure? **When comparing two methods, change exactly one axis at a time** — changing model + optimizer (+ preprocessing) makes the result unattributable. Every improvement must answer: "does this gain come from representation, evaluation, or optimization?" If it cannot, the experiment is a black box.

---

## 3. Representable does not imply learnable (L11)

**Core**: That a hypothesis space *can represent* a target function does not mean the optimization algorithm will *find* it with finite data and time. Because of local optima, search strategy, and data quantity, what is actually learned is only a small part of the representable space.

**Trap**: Arguing "this method works" from "the model class has enough capacity" is a leap.

**Operational translation**: Negative results must be attributed first — did the method fail because it **cannot represent** (capacity/features) or because it **cannot learn** (optimization failure: local optima, learning rate, initialization, data size)? Misattribution turns a fixable problem (e.g., a better optimizer) into a verdict on the whole approach. "Representable in theory" is motivation, not evidence.

---

## 4. Evaluation discipline

### 4.1 It's generalization that counts (L2)

**Core**: The goal is performance on **unseen test data**; training error is only a proxy for test error. Evaluating directly on the training set is the "training-set success illusion." Repeatedly tuning on the test/validation set silently leaks test-set information into the model and invalidates the generalization estimate.

**Trap**: Evaluation pollution is cumulative and silent — the more you tune, the more the validation set behaves like the training set.

**Operational translation**: On day one, carve out an **independent, frozen final test set** that no choice (method, hyperparameter, feature) ever touches; budget the number of tuning runs on the validation set; label every reported number with the set it came from. This is a hard check item for the measure module.

### 4.2 Overfitting has many faces (L4)

**Core**: Overfitting is the trade-off between **bias** (tendency to systematically learn the wrong thing) and **variance** (tendency to learn random things as data fluctuates). **Noise is not required for overfitting** — on noise-free data, complex models (e.g., disjunctive normal form) can still overfit badly. **Strongly wrong assumptions can beat weakly right ones**: a strongly-assuming simple model (e.g., naive Bayes) often beats a more realistic but high-variance complex model on limited data, because of low variance.

**Trap**: "Strongly wrong > weakly right" is the norm on finite data, not a surprise.

**Operational translation**: When performance is poor, diagnose first — **high bias** (underfitting → add capacity/features) or **high variance** (overfitting → add data/regularization/ensembling)? A wrong diagnosis points the remedy in the wrong direction. Strong-assumption simple baselines (naive Bayes, linear models) are legitimate, often hard-to-beat scientific references — not "we didn't do real methods." Report variance across seeds, not a single run.

### 4.3 Theoretical guarantees are not what they seem (L6)

**Core**: Theoretical bounds (e.g., PAC learning bounds) show induction can have probabilistic guarantees, but the bounds are usually **pessimistic**: required sample sizes often grow exponentially, while in practice a tiny fraction of that suffices for high accuracy. Asymptotic guarantees (convergence as data → ∞) give limited guidance in finite-data settings.

**Trap**: Theory says "you need this many samples," practice says "you don't" — both are true; theory is not the sole basis for choosing an algorithm.

**Operational translation**: Theory is intuition and motivation; **empirics are the arbiter**. When citing a bound, state its looseness; "theoretically motivated" methods still need experimental validation. In the literature module, default to discounting "proven in theory" claims until experiments support them.

---

## 5. The data levers

### 5.1 Data alone is not enough (L3)

**Core**: The **No Free Lunch theorem** — with no prior assumptions, no algorithm beats random guessing across all problems. Induction must leverage prior knowledge / domain assumptions (smoothness, similar inputs → similar outputs, ...) to turn limited inputs into generalization.

**Trap**: More data ≠ automatic learning; without inductive bias, more data is just more memorization.

**Operational translation**: Every method embeds priors — **"which assumption is this method leaning on to succeed?" is the most productive question in research**. Report a method's inductive bias explicitly; "purely data-driven, no assumptions" is itself a false narrative. When a method fails in a new domain, its priors usually don't hold there — that is a research opportunity, not a bug.

### 5.2 Intuition fails in high dimensions (L5)

**Core**: As dimensionality grows, samples become sparse and distance/similarity-based methods break down (the **curse of dimensionality**): in high dimensions most points lie on the surface of the space and all pairwise distances look alike. The saving grace is the **blessing of non-uniformity** — real data concentrates near a lower-dimensional manifold, which keeps high-dimensional learning feasible in practice.

**Trap**: Adding features is not information-only-up; 2D/3D intuitions are void in high dimensions.

**Operational translation**: For high-dimensional experiments (embeddings, feature explosion), sanity-check the distance distribution first; justify manifold assumptions with evidence; treat the feature-dimension budget as a first-class design resource.

### 5.3 More data beats a cleverer algorithm (L8)

**Core**: In practice, the fastest fix for a performance bottleneck is usually **more data**, not a more complex algorithm. "Dumb algorithm + lots of data" routinely beats "clever algorithm + little data." Since all algorithms ultimately separate by proximity to nearby examples, their predictions converge as data becomes rich.

**Trap**: Chasing complex algorithms costs compute time and tuning effort, with diminishing returns as data grows.

**Operational translation**: When performance plateaus, **add data / plot a learning curve** (performance vs. data size) before switching algorithms. Claims like "X beats Y" must state the data regime — conclusions that hold at small scale can invert at scale. Learning curves should be a standard artifact of the measure module.

---

## 6. Feature engineering is the key (L7)

**Core**: Most of a project's effort should go into feature construction, cleaning, and transformation; **simple models + good features** routinely solve problems that complex models struggle with. Individually irrelevant features can be highly predictive in combination (the XOR problem: each single feature has zero information gain, yet the combination is decisive).

**Trap**: ML is not one-click automation; filtering features by individual significance systematically misses combinatorial effects.

**Operational translation**: Budget explicit time for feature engineering; feature selection must check combination/interaction effects — never filter by single-feature significance alone; "this feature is useless" is only a conclusion after interaction checks.

---

## 7. Model strategy: ensembles and simplicity

### 7.1 Learn many models, not just one (L9)

**Core**: Combining multiple models (**ensembles**: Bagging / Boosting / Stacking) usually performs far better than picking the single best model. Do not confuse ensembles with **Bayesian model averaging (BMA)** — BMA weights collapse onto one dominant model, whereas ensembles reduce both variance and bias.

**Trap**: Spending effort on choosing "the best algorithm" is worse than fusing the predictions of several.

**Operational translation**: Include an ensemble baseline (random forest / boosting) in experiments by default; single-model claims must hold against ensemble comparison; when claiming "our method beats X," X should be a strong version of its method class.

### 7.2 Simplicity does not imply accuracy (L10)

**Core**: Occam's razor is often misread as "given equal training error, the simpler model generalizes better." **Parameter count has no necessary link to overfitting**: boosting ensembles keep improving test error as trees (parameters) multiply; SVMs have infinite capacity yet resist overfitting. Simplicity is preferred because **simple is easier for humans to understand**, not because simple is more accurate.

**Trap**: Arguing model choice with "fewer parameters, therefore better" is a misapplied razor.

**Operational translation**: Defend simple models on **interpretability/maintainability** grounds, not accuracy; "simplicity improves generalization" requires experimental evidence; the present module's "why this model" passage follows this discipline.

### 7.3 Representable does not imply learnable (L11)

See §3 — grouped there with the evaluation discipline because it governs how to read negative results.

---

## 8. Correlation does not imply causation (L12)

**Core**: Most ML algorithms extract **correlations** from observational data; they cannot establish **causation**. Acting on predictive associations ("people who buy diapers often buy beer" → rearrange the store) may not work; verifying an intervention requires experimental data (e.g., A/B tests) or causal-inference frameworks.

**Trap**: "Important features" from a predictive model are often treated as "intervention levers" — the most expensive category error in the field.

**Operational translation**: Distinguish two kinds of research questions — **prediction** (correlation suffices) vs. **decision/intervention** (causal evidence required). Causal language in a report ("increasing X improves Y") requires experimental evidence; a benchmark improvement is a correlation-level claim and cannot be translated directly into product advice. The verify module should include a "causal-language audit" check item.

---

## 9. Mapping to ava-serious-research modules

| Module | Contribution |
|--------|--------------|
| question | Prediction vs. causal question split (L12); anchor questions in a data regime (L8); No Free Lunch → problem statements need explicit priors (L3) |
| literature | Read theory claims with discount, empirics first (L6); stay skeptical of "simplicity = good" arguments (L10) |
| design | Make representation/evaluation/optimization explicit + one-axis-at-a-time comparison (L1); strong-assumption simple baselines (L4); ensemble baselines (L9); declare inductive bias (L3) |
| measure | Independent frozen test set + tuning budget (L2); bias/variance diagnosis (L4); learning curves (L8); feature interaction checks (L7); multi-seed variance (L4) |
| reproduce | Label eval-set identity, log tuning runs, record data size and preprocessing (L2/L8) |
| present | "Simple = comprehensible," not "simple = more accurate" (L10); theory as intuition, not evidence (L6); causal-language discipline (L12) |
| verify | Test-set pollution audit (L2); negative-result attribution: representation vs. learning (L11); theory vs. empirics reconciliation (L6); causal-language audit (L12) |

---

## 10. Candidate checklist items (for the skill)

- [ ] MUST Carve out an independent, frozen final test set on day one; no choice ever touches it (L2)
- [ ] MUST Label every evaluation number with its set identity (train/val/test); budget tuning runs (L2)
- [ ] MUST When comparing methods, change exactly one axis (representation/evaluation/optimization/data/preprocessing) (L1)
- [ ] MUST Diagnose bias vs. variance before choosing a remedy for poor performance (L4)
- [ ] MUST Report multi-seed variance, not a single run (L4)
- [ ] MUST State the priors/inductive bias a method depends on (L3)
- [ ] MUST Attribute negative results ("cannot represent" vs. "cannot learn") before concluding (L11)
- [ ] MUST Avoid causal language for predictive findings; intervention advice requires experimental evidence (L12)
- [ ] SHOULD Include a strong-assumption simple baseline (naive Bayes / linear) and compare against it (L4)
- [ ] SHOULD On a performance plateau, plot a learning curve / add data before switching algorithms (L8)
- [ ] SHOULD Check feature combination effects, not single-feature significance alone (L7)
- [ ] SHOULD Include an ensemble baseline in comparisons (L9)
- [ ] SHOULD Defend simple models on interpretability, not accuracy (L10)
- [ ] SHOULD Sanity-check distance/similarity methods in high-dimensional settings (L5)
- [ ] SHOULD Flag theoretical claims as loose; conclusions rest on empirics (L6)
