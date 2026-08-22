# 06 · Conjectures and Refutations — Reference

**Author**: Karl R. Popper (1963; full title *Conjectures and Refutations: The Growth of Scientific Knowledge*)
**Source**: Routledge (1963); Chinese translation 《猜想与反驳》.
**Role in this skill**: the epistemological anchor of `principles/falsifiability` and `principles/honesty`; the philosophical basis of the question-outcome typology (answers / better-posed confusion / reframed questions) and of refutation-first experiment design in `practices/question` and `practices/verify`.
**Review status (Chinese edition)**: ⏳ pending user review (2026-08-07, epistemology supplement group).

## 0. One-line core

> Science is not the accumulation of confirming evidence but **bold conjecture + relentless refutation**: the demarcation line is falsifiability, not verifiability; we can only guess, but we can submit our guesses to criticism and thereby approach the truth; the growth of knowledge shows up not as more answers but as **better problems** and a more articulate awareness of our own ignorance.

## 1. Demarcation: falsifiability, not verifiability

- The line between science and pseudoscience is not "has evidence" but **what observation could refute it**. Astrology and unfalsifiable psychoanalytic narratives can always "explain" anything — which is exactly why they are not science.
- Falsifiability is not "this theory is wrong" but "this theory dares to be tested": a claim must carry the observation that would prove it wrong.
- **Operationalization for ML research**: this is the origin of this skill's falsifiability rule — "Predict: component A carries the gain. If ablating A leaves less than a 1-point gap, this hypothesis fails." A hypothesis without a stated refuting observation is not a hypothesis.

## 2. Growth of knowledge = conjecture and refutation

- There is no presuppositionless observation: all observation is theory-laden. We see the world through conjectures and revise them by refutation.
- Confirmation and refutation are asymmetric: a thousand successful predictions cannot prove a theory; one failure can topple it. **Looking for refutation is closer to science than looking for support.**
- **Operationalization**: invert the priority of experiment design — run the experiment most likely to *topple* the hypothesis first (this is the theoretical source of `practices/question`'s minimal decisive experiment); when auditing, ask "what result would change my mind" before asking "does the result support me".

## 3. Truth and ignorance: why more confusion is progress

- Science aims at truth (Popper's *verisimilitude*), but every theory is a provisional conjecture — **"our knowledge can only be finite, while our ignorance must necessarily be infinite."**
- The key claim (answering the critique that research should aim at truth, not at "finding something out"): **the more we learn about the world, the more specific and articulate our knowledge of our ignorance becomes** — the map of ignorance grows with knowledge.
- **Operationalization**: three kinds of research outcome are all legitimate: (a) an answer; (b) **better-posed confusion** — turning a vague "something is off" into a testable "here is the contradiction"; (c) a reframed question — Y was the wrong question, the real question is Y'. A report that honestly articulates what it does not yet understand can be worth as much as an answer.

## 4. Critical rationalism: rationality = willingness to be criticized

- Rationality is not "deriving conclusions correctly" but **exposing one's views to criticism and being ready to revise them**; someone who never criticizes their own position is not being rational.
- The boundary of tolerance: what we do not tolerate is claims that refuse to be tested (unfalsifiable claims have no cognitive status).
- **Operationalization**: adversarial self-review (`practices/verify`) is not a formality — it is critical rationalism made operational; the "whoever renders the frame must not be the one to declare it done" separation (`practices/present`) guarantees that criticism actually happens.

## 5. Contribution mapping to ava-serious-research

| Module | Contribution |
|--------|--------------|
| question | "Better problems" as the measure of progress; philosophical basis of the outcome typology (a)/(b)/(c); the deep justification of the So-what test |
| design | Refutation-first: run the experiment most likely to topple the hypothesis (source of minimal decisive experiment) |
| measure | Asymmetry of refutation: a failed test carries more information than a successful one; report must state what would refute the claim |
| verify | Adversarial self-review = critical rationalism operationalized; seek refutation before support |
| present | Conclusions framed as "best current conjecture," never final truth |
| honesty | "We can only guess" — admitting ignorance and uncertainty is the scientific posture, not weakness |

## Checklist candidates

- [ ] MUST every hypothesis carries the observation that would refute it (demarcation)
- [ ] MUST experiment plan prioritizes the test most likely to refute the hypothesis
- [ ] MUST observations contradicting the hypothesis are reported, not hidden
- [ ] MUST conclusions are qualified as "under current evidence," never final truth
- [ ] SHOULD the study states which part of the ignorance map it updated (what new unknowns it exposed)
- [ ] SHOULD the study distinguishes "a conjecture was refuted" from "a conjecture was proven"
