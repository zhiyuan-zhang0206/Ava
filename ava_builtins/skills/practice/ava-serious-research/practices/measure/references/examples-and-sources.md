# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Raschka, *Model Evaluation, Model Selection, and Algorithm Selection in Machine Learning* (arXiv:1811.12808) — three goals, McNemar / 5x2cv / Cochran's Q + Bonferroni, decision table (`../../../references/04-raschka.md`)
- Ng, *Machine Learning Yearning* — single-number metric, optimizing/satisficing, Eyeball/Blackbox dev sets, error analysis ceilings, bias/variance/data-mismatch decomposition, learning curves (`../../../references/02-mlyearning.md`)
- Gelman & Loken, *The Garden of Forking Paths* — researcher degrees of freedom
- Kapoor & Narayanan, *Leakage and the Reproducibility Crisis in ML-based Science* — inflated metrics as leakage artifacts (`../../../references/05-kapoor.md`)
- Luo, Kasirzadeh, Shah, CMU evaluation of AI Scientist (arXiv:2509.08713) — automated p-hacking via test-set-aware reward — evidence also in `ai-era/ai-failure-modes`
- Dietterich (1998), approximate statistical tests for comparing supervised classification learning algorithms — cited via Raschka
