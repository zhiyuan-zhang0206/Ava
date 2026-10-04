# 08 · The Beginning of Infinity — Reference

**Author**: David Deutsch (2011; full title *The Beginning of Infinity: Explanations That Transform the World*)
**Source**: Allen Lane / Viking (2011); Chinese translation: The Beginning of Infinity.
**Role in this skill**: the teleology of the whole skill family — the mission's purpose chain (truth → understanding → human flourishing) and the philosophical criterion behind anti-overfitting: a good explanation is hard to vary. Foundational for `practices/question`'s outcome typology, `principles/parsimony`, and `practices/present` (explanation as first-class output).
**Review status (Chinese edition)**: ⏳ pending user review (2026-08-07, epistemology supplement group).

## 0. One-line core

> Progress is driven by one engine: the growth of **good explanations**. Explanations — not predictions, not data, not authority — are the unit of knowledge. Problems are inevitable and in principle solvable; solving a problem necessarily creates more problems, but the new problems are **better ones** — this escalation is why knowledge growth has no end (the beginning of infinity) — and the growth of knowledge ultimately serves human flourishing.

## 1. Good explanations: the unit of knowledge (and the criterion against overfitting)

- A good explanation is **hard to vary**: its parts interlock, and changing any of them would collapse the whole; a bad explanation can be arbitrarily adjusted to fit any observation.
- This criterion hits overfitting directly: **an account that can be freely adjusted to fit the data is not an explanation — it is curve fitting.** A real explanation must be hard to vary, i.e., changing it would stop explaining known facts.
- **Operationalization for ML research**: a model is not an explanation; the explanation is the mechanistic statement of *why* the model works on the target distribution. When evaluating a research conclusion, first ask: how hard to vary is this explanation? If it can be revised on the next dataset, it is not yet knowledge.

## 2. Explanation vs prediction vs data

- Successful prediction ≠ understanding: a black box can predict everything and explain nothing (Deutsch's fundamental critique of purely data-driven approaches).
- The goal of science is to explain the world, not to catalogue phenomena; instruments (predictors, benchmarks) serve explanation, they do not replace it.
- **Operationalization**: reports make the mechanism/explanation the first-class output, with correlations/predictions as evidence rather than endpoints; "X correlates with Y" is not a conclusion — "because of mechanism Z, X causes Y" is (echoing Domingos's causal lesson).

## 3. Problems are inevitable and solvable

- **Problems are inevitable**: every solution creates new problems; there is no final solution.
- **Every problem is in principle solvable**: the optimism principle — bad things come from lack of knowledge, and knowledge can grow.
- The key claim (the final answer to "research can increase confusion"): **new problems are not a mark of failure but the measure of progress** — the "better problems" list is the scoreboard of research.
- **Operationalization**: outcome types (b) and (c) receive their full justification here: better-posed confusion = problem upgrade; a question reframed into testable form is a deliverable.

## 4. The purpose chain: knowledge → understanding → human flourishing

- The growth of knowledge is humanity's only true engine of progress: wealth, health, freedom, and happiness all derive from it.
- Research is therefore not an academic game but the mechanism of human flourishing itself — a terminal answer to "why do research at all" (the positive construction behind the critique that research should serve truth → understanding → benefit → happiness).
- **Operationalization**: the deep standard for question selection: after this question is answered, how much understanding advances, and what does it serve? The final version of the So-what test is "whose life or cognition does this knowledge make better, and how."

## 5. Contribution mapping to ava-serious-research

| Module | Contribution |
|--------|--------------|
| mission | Philosophical source of the purpose-chain statement (truth → understanding → human flourishing) |
| question | Full justification of outcome typology (a)/(b)/(c); "better problems" as the scoreboard |
| design | Hard-to-vary explanations = the philosophical criterion against overfitting (explanation robustness) |
| measure | Metrics serve explanation, not replace it; prediction ≠ understanding |
| present | Explanation/mechanism as first-class output, prediction as evidence |
| verify | "Where would this explanation break if varied?" (sensitivity/robustness audit) |
| honesty | Problems are inevitable → honestly reporting new problems, not dressing up "complete solution" |

## Checklist candidates

- [ ] MUST conclusions are presented as explanations (mechanism/causal chain), not merely correlations or predictions
- [ ] MUST the explanation passes the hard-to-vary test: which part would collapse if changed? Any freely adjustable components?
- [ ] MUST the study lists the new problems it created (the better-problems list)
- [ ] SHOULD the study states which layer of understanding it advanced (phenomena / mechanism / theory)
- [ ] SHOULD the study states what this knowledge ultimately serves (purpose chain: who benefits, how)
- [ ] SHOULD model and explanation are presented separately: the model is a tool, the explanation is the output
