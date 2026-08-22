---
name: falsifiability
description: "Use when formulating a research question or hypothesis, before designing experiments — a claim only counts as research if it can be wrong: every claim must carry the observation that would refute it and the experiment that could produce that observation."
---

# Falsifiability

## One-sentence core
> A research question only counts as research if it can be falsified: every claim must carry an explicit "what observation would make me give it up or revise it", plus the experiment that could produce that observation.

## Core principles
- **Claims carry refutation conditions**: each claim implies at least one observable outcome that, if it occurred, would force revision or retraction — **Why**: an unfalsifiable claim can be neither verified nor learned from, and it ends the conversation with the research community (the shared basis of the So-what test and auditable question design in `practices/question`) — **How**: when writing a claim, also write its counterfactual experiment: "if experiment X yields result Y, this claim does not hold".
- **Hypotheses before experiments**: record hypothesis, prediction, and judgment criteria before running anything — **Why**: post-hoc hypotheses are HARKing; the researcher-degrees-of-freedom literature (Garden of Forking Paths) shows this is a primary source of systematic bias — **How**: before any comparison, write one line: prediction + what counts as support / refutation.
- **Minimal decisive experiment**: design the smallest experiment that distinguishes your hypothesis from the strongest alternative explanation — **Why**: fewer entangled variables, cleaner attribution (Domingos L1: change one axis at a time) — **How**: for each hypothesis ask "what is the single experiment most likely to shake it" and run that first.
- **Pre-register the adjudication rule**: decide in advance what evidence settles the question — **Why**: without a pre-set rule any outcome can be read as support; retrospective reading is HARKing, the degrees-of-freedom problem at the heart of the Garden of Forking Paths — **How**: before the run, write the decision rule: 'if metric M moves ≥δ on the sealed test set, the hypothesis is supported; otherwise not'.
- **Claims carry boundaries**: a claim must state the conditions under which it holds — **Why**: unbounded claims are neither falsifiable nor defensible (claim qualifiers; Kapoor: generalization claims require independent external validation) — **How**: scope every conclusion ("under ≤N parameters / on dataset D / within distribution P").

## Checklist
- [ ] Every claim carries a "observe X → give it up or revise" statement
- [ ] The research question passes the So-what test (who loses what if the answer is missing)
- [ ] The experiment can distinguish the hypothesis from the strongest alternative explanation
- [ ] Prediction and judgment criteria written before the comparison run
- [ ] Every conclusion states its scope (distribution / scale / setup boundaries)
- [ ] Generalization claims have independent external validation, otherwise downgraded to "valid within the collected distribution"
- [ ] The adjudication rule (metric + threshold + which set) was written before the run, not after

## Anti-patterns
- **Unfalsifiable contribution**: "our method is more robust/general" with no boundary and no experiment → instead: define robustness operationally and give the refutation condition
- **Post-hoc rationalization**: writing the hypothesis after seeing results → instead: pre-register; mark any post-hoc revision as a revision
- **Confirmation-biased topic choice**: only pursuing directions expected to succeed → instead: put "the experiment most likely to refute me" near the top of the queue
- **Benchmark number as claim**: leaderboard chasing is not a claim → instead: claims are statements about phenomena; benchmarks are evidence
- **Vague adjudication**: 'we'll see if it works' — no pre-set threshold or criterion → instead: pre-register the decision rule before running

## Bad → good
- **bad**: "The approach seems promising." (no claim, no test, nothing to refute)
- **good**: "Hypothesis: retrieval augmentation helps on long-tail queries. Adjudication rule: ≥3-point recall gain on the sealed test set over the base model, else the hypothesis fails."
- **bad**: "Our method is better than the baseline." (no boundary, no refutation condition)
- **good**: "Under ≤10M parameters and a fixed search budget, our method exceeds the baseline F1 by 3.2±0.4 points on dataset X; if removing component A eliminates the gain, this claim does not hold. The conclusion is scoped to X's collection distribution; cross-distribution generalization is unverified."
- **bad**: After running, "we can actually claim B" (post-hoc hypothesis)
- **good**: Before running: "Prediction: component A drives the gain; if ablating A yields <1 point, the hypothesis fails." Verified against the record afterwards.

## Relationships
- Protocols and leakage defenses: `practices/design`; statistical adjudication and pre-registration mechanics: `practices/measure`
- The boundary discipline that turns a refuted hypothesis into a scoped claim: `principles/claim-evidence-alignment`
- Presentation duty for hypotheses: `practices/present`; self-refutation audit: `practices/verify`
- "Post-hoc rationalization" shares its root with p-hacking: `principles/honesty`

## Sources
- Popper, *Conjectures and Refutations*, 1963 — `../../references/06-poppers-conjectures-and-refutations.md` (falsifiability as demarcation; refutation-first)
- Gelman & Loken, The Garden of Forking Paths
- Domingos, A Few Useful Things to Know About ML (L1)
- Kapoor & Narayanan, Leakage and the Reproducibility Crisis in ML-based Science
