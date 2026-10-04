---
name: parsimony
description: "Requires every component, parameter, model choice, and claim embellishment to earn its complexity through evidence. Use when adding architecture, choosing a model, interpreting ablations, or writing an explanation that may be more elaborate than the data supports."
---

# Parsimony

## One-sentence core
> Complexity must pay rent: every component, parameter, and claim embellishment justified by ablation or neutral comparison — where a simple method suffices, a complex one earns nothing by existing.

## Core principles
- **Complexity must be proven by ablation**: every added component needs "removing it makes the gain disappear" evidence — **Why**: component stacking is the easiest fake source of improvement (Domingos L1: attribution requires changing one axis at a time); complexity without ablation cannot be located — **How**: any modular method ships an ablation table: full / -A / -B / -A-B.
- **Strong simple baselines first**: complex methods first compare fairly against strong simple ones — **Why**: Kapoor's civil-war prediction replication: every "complex ML beats LR" paper was irreproducible due to leakage, and complex models did not substantively win; Boulesteix calls for neutral comparison studies — **How**: the baseline is a well-tuned strong simple method (logistic regression / linear model / rule baseline) under the same protocol as the main method.
- **Claim complexity ≤ evidence complexity**: the number of qualifiers in a claim does not exceed what evidence supports — **Why**: Ockham's razor applied to argument: redundant qualifiers are noise like redundant components (qualifier discipline) — **How**: after writing a claim, delete every adjective no experiment directly supports.
- **Data and compute are not free**: gains bought with more data/compute are attributed honestly — **Why**: a comparison is informative only when the compared methods ran under the same budget; otherwise the difference conflates method with resources. This is the field's recurring lesson — the Bitter Lesson: generic methods that exploit compute have repeatedly beaten clever hand-crafted ones — so the honest question is always which you are actually measuring. (The 2026-08 observation that model scale alone raises AI-research quality is in `ai-era/ai-research-landscape`.) — **How**: fix budgets (data/compute/parameters) in comparisons, or report scaling curves explicitly.

## Checklist
- [ ] Every component has ablation evidence (gain disappears or drops significantly when removed), recorded in the ablation table
- [ ] Baseline is a strong simple method under the same protocol as the main method
- [ ] Comparisons fix budgets (data/compute/parameters) or report scaling curves
- [ ] Claims have no redundant qualifiers; every modifier is experiment-supported
- [ ] The minimal method that reproduces the main result is identified and compared (strong simple baseline)
- [ ] Every component in the final claim has ablation evidence; the rest are removed or labeled exploratory

## Anti-patterns
- **Component stacking**: "we added A, B, C so it's better" (no ablation) → instead: ablate component by component; keep only what has evidence
- **Strawman baseline**: complex model vs an untuned simple model → instead: strong simple baseline + fair protocol (Boulesteix neutral comparison)
- **Scale masquerading as method**: buying gains with more data/compute, claiming a method improvement → instead: fixed-budget comparison or explicit scaling attribution
- **"Complexity is depth"**: treating method complexity as a contribution in itself → instead: contribution = verifiable gain, not complexity
- **Complexity hoarding**: keeping components "just in case", with no evidence they earn their place → instead: remove anything without ablation support from the final claim

## Bad → good
- **bad**: "We used 10× more training data than the baseline, and we win." (resource difference conflated with method)
- **good**: "We match the baseline's data/compute budget and win; when the baseline gets our budget, the gap shrinks to 1.1 points — the win is partly scale, and we say so, attributing the rest via ablation."
- **bad**: "We propose an A+B+C fusion framework, +8 points" (nobody knows which component works)
- **good**: "Full +8.0; -A +1.2; -B +7.1; -C +7.8 → the gain comes from A; B and C are not significant (paired test p>0.05); B and C removed in subsequent experiments."
- **bad**: adding ensembling, distillation, and a new scheduler at once; the combination wins, nobody knows why
- **good**: each addition is measured alone against the previous best; only additions with isolated, significant gains stay
- **bad**: new method vs default-parameter LR (LR untuned)
- **good**: new method vs tuned LR + same cross-validation protocol, learning curves for both

## Relationships
- Ablations and baselines: `practices/design`; statistical significance: `practices/measure`
- Budget attribution and the 2026 scaling observation: `ai-era/ai-research-landscape`
- The neutral-comparison movement that motivates strong baselines: references/03-domingos.md + references/05-kapoor.md (civil-war replication)
- Time-sensitive observations on scaling: `ai-era/ai-research-landscape`
- With honesty: a strawman baseline is an integrity problem as much as a simplicity problem — `principles/honesty`

## Sources
- Domingos, A Few Useful Things to Know About ML (L1, L9, L10)
- Boulesteix, A Plea for Neutral Comparison Studies in Computational Sciences
- Kapoor & Narayanan, Leakage and the Reproducibility Crisis in ML-based Science (civil-war replication case)
- Sutton, The Bitter Lesson; AI-era scaling observation: `ai-era/ai-research-landscape`
