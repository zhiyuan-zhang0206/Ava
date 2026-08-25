---
name: question
description: Converts vague research interests into consequential questions, falsifiable hypotheses, and an auditable question certificate. Use before writing code or running experiments for any new research project, even when the topic already sounds specific.
---

# Question Formation & Hypotheses

## One-sentence core
> A research project begins with a question, not a topic: *"I am studying X because I want to find out Y, in order to help my audience understand Z"* — and the question only counts if its answer being missing would cost someone something, and it carries a hypothesis that specifies the observation that would refute it.

## Core principles
- **Three-part question formula**: State the question as "studying X to find out Y, so that the audience understands Z" — **Why**: X forces a vague interest into a focused topic, Y turns the topic into an answerable question, and Z is the "so what" that ties the question to a reader who would be affected. Most failed projects never complete Z. — **How**: Write the sentence in one line in your project file; rewrite until Z names a concrete audience and a concrete consequence. If you cannot name Z, you have a topic, not a research question.
- **The So-what test**: If the answer were missing, who would care, and what would they lose? — **Why**: a question is worth researching when the audience suffers a loss from not knowing the answer; a question whose absence harms nobody is not research. In ML terms, a new SOTA number on a benchmark nobody depends on fails this test. — **How**: Write the counterfactual: "If nobody answers this question, then ___ loses ___." If the blank is empty, the question is benchmark-padding — extend, qualify, or challenge an existing answer instead (the three ways to build on prior work: extend, qualify, challenge).
- **Question typology before evidence standard**: Classify the question — practical (what to do) vs conceptual (how to think); and for ML specifically, prediction (does X correlate with Y?) vs intervention (does changing X cause Y?). — **Why**: The type fixes the evidence standard: practical questions need decision-grade evidence, conceptual questions need understanding; prediction claims need only correlation, intervention claims need causal evidence — Domingos's Lesson 12 (correlation does not imply causation) is the most expensive boundary violation in applied ML research. — **How**: Write the classification next to the question and derive the evidence bar it implies: "this is a prediction question, so benchmark evidence suffices, and causal language is forbidden unless a causal experiment is added."
- **Falsifiable hypothesis with a pre-registered decision rule**: Before running anything, write the hypothesis, the prediction, and what result counts as support vs refutation. — **Why**: The Garden of Forking Paths shows that many "legitimate" analyses exist after the fact; a hypothesis written after seeing results is not a hypothesis (HARKing). The falsifiability principle requires every claim to carry the observation that would force its revision. — **How**: Write one dated line: "Predict: component A carries the gain. If ablating A leaves less than a 1-point gap, this hypothesis fails." Date-stamp it in the design doc before the first run.
- **Minimal decisive experiment**: For each hypothesis, identify the single experiment most likely to topple it and run it first. — **Why**: Domingos's Lesson 1: to attribute an outcome you change exactly one axis at a time; the decisive experiment isolates the hypothesis from its strongest alternative. — **How**: Ask "what is the strongest alternative explanation for the effect I predict, and what experiment would distinguish them?" Put that experiment first in the plan, even when it is less flashy.
- **Research outcome typology — answers, better-posed confusion, reframed questions**: A research project legitimately ends in one of three outcomes: (a) an answer to the question; (b) **better-posed confusion** — turning a vague "something is off" into a testable "here is the contradiction" (Popper: the more we learn, the more articulate our knowledge of our ignorance; Kuhn: discovery commences with the awareness of anomaly); (c) a **reframed question** — Y was the wrong question, the real question is Y'. — **Why**: the linear question→answer model (Craft of Research) understates real research: progress is better problems, not merely more answers (Popper, Conjectures and Refutations; Deutsch, The Beginning of Infinity); treating confusion as failure pushes researchers to fake closure or hide anomalies. — **How**: Before starting, write which outcome type the project is *aiming* for and which it would honestly *accept*; when a result raises more questions than it answers, report the upgraded problems explicitly — that is progress, and it belongs in the deliverable, not in the footnotes. For outcome (b), the So-what test becomes: "who loses if this contradiction stays unsharpened?"
- **Auditable question certificate**: Keep a traceable derivation of the question — raw interest → tension in the literature → research question → hypothesis → mechanistic model → decisive test → failure-update rule — each field checkable before execution. — **Why**: an argument is only as sound as its premises, and premises you can inspect are premises a collaborator can correct — a checkable derivation is what lets a human verify and steer before effort is spent (claims, reasons, and warrants must be explicit). The seven-field certificate is a 2026 operationalization of this from auditable-question research — FirstResearch (arXiv 2607.05682), withdrawn by its authors; take the idea, not the citation — and the AI Research Assistant analysis (arXiv 2602.22842) independently finds AI weak at choosing questions and strong at executing, so the certificate is where human judgment meets the agent's work (`ai-era/ai-research-landscape`). — **How**: Maintain a certificate file with the seven fields; before the first experiment, verify each field is filled and human-checkable; record the failure-update rule ("if the decisive test gives R, the hypothesis is revised to ...").

## Checklist
- [ ] The research question is written in three-part form (studying X / find out Y / audience understands Z)
- [ ] So-what test passed: a named audience and a concrete loss if the answer stays missing
- [ ] Question classified (practical/conceptual; prediction/intervention) and the evidence standard for that class written down
- [ ] At least one hypothesis written and date-stamped before any experiment, with a support/refutation decision rule
- [ ] The minimal decisive experiment against the strongest alternative is identified and scheduled first
- [ ] Question certificate fields filled and human-checkable (interest, tension, question, hypothesis, mechanism, decisive test, failure-update rule)
- [ ] Each hypothesis carries its qualifiers — the distribution/setting in which it is claimed to hold
- [ ] If the question builds on an existing answer (extend/qualify/challenge), that answer is cited and the assumption being challenged is named
- [ ] Claimed novelty is stated against the closest prior work, with the differentiating experiment named

## Anti-patterns
- **Benchmark padding**: "We beat SOTA on dataset B by 2 points" — nobody loses if it stays unanswered → Replace: state the phenomenon claim and name the audience that loses.
- **Vague curiosity**: "I want to understand transformers" — a topic, not a question → Replace: narrow through interest → topic → question → research question until Y is answerable.
- **Post-hoc hypothesis (HARKing)**: results first, hypothesis second → Replace: pre-register; label any later revision as a revision.
- **Unfalsifiable framing**: "our method is more robust / more general" with no boundary → Replace: define robustness operationally and state the refuting observation (`principles/falsifiability`).
- **Prediction evidence wearing causal clothes**: "these features predict X, therefore raising X improves Y" → Replace: classify as prediction, or design the causal experiment (Domingos L12).
- **Question soup**: five research questions in one project → Replace: one primary question; the rest become explicit secondary/exploratory items.

## Bad → good
- **bad**: "We study LoRA fine-tuning of code LLMs." (a topic — no Y, no Z)
- **good**: "I am studying LoRA fine-tuning on code LLMs (X) to find out whether rank determines where the gain comes from — data fidelity vs optimization ease (Y) — so that practitioners can pick rank by their actual bottleneck (Z)." Then the hypothesis: "at low data budgets, rank gains come from optimization ease; refutation: holding budget fixed, low-rank matches high-rank under the same optimizer settings."
- **bad**: "Our method achieves 92.3% on benchmark B, +2.0 over SOTA." (no one loses if it never existed)
- **good**: "Question: does multi-stage training reduce hallucination on out-of-distribution queries? Hypothesis: the gain comes from stage-2 data mixing, not scale; if we hold compute fixed and remove stage-2, the gap disappears. So-what: OOD hallucination is the reported production failure for this model family; a recipe that fixes it removes a known deployment blocker."

## Relationships
- The question certificate's 2026 operationalization and the AI tool landscape around it: `ai-era/ai-research-landscape`

- Finding the tension that feeds Y: `practices/literature` (the product of a literature review is a live tension, not a bibliography)
- Turning the decisive experiment into a protocol: `practices/design`; falsifiability discipline: `principles/falsifiability`
- Pre-registration and the statistics of the decision rule: `practices/measure`
- HARKing and selective reporting as integrity issues: `principles/honesty`
- The qualifiers every claim carries: `principles/claim-evidence-alignment`
- AI-era note: AI systems are poor question-choosers (arXiv 2602.22842) — question formation stays human-led; see `ai-era/ai-research-landscape`

## Sources
- Popper, *Conjectures and Refutations*, 1963 — `../../references/06-poppers-conjectures-and-refutations.md` (better problems as progress; outcome typology)
- Deutsch, *The Beginning of Infinity*, 2011 — `../../references/08-deutsch-beginning-of-infinity.md` (problems are inevitable and solvable; better problems as the scoreboard)
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../references/03-domingos.md` (L12 prediction vs causation)
- Gelman & Loken, "The Garden of Forking Paths" (2013) — post-hoc analysis paths
- FirstResearch, "Auditable Question Formation for LLM Scientific Discovery Agents," arXiv 2607.05682 — withdrawn by authors; question-certificate design idea only, not an established result — evidence also in `ai-era/ai-research-landscape`
- The AI Research Assistant, arXiv 2602.22842 (human-led question setting; multi-assistant + human-led pattern) — evidence also in `ai-era/ai-research-landscape`
