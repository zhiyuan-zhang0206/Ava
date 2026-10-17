---
name: evaluate
description: "Designs evidence-based evaluation with limited human attention. Use to discover failures, inspect traces, calibrate judges, or compare changes when quality is uncertain."
---

# Evaluate — Improve the Work and the Judgment

Evaluation should support a decision and improve the ability to make the next
one. Its subjects include a system's behavior, an artifact, a working method,
and human or agent beliefs about them. "Anything" describes the range of
subjects, not a universal score or grader.

For a small task with an objective answer, perform the direct check and report
it. Do not add a judge, an interview, a review interface, or a team merely
because evaluation is possible.

## Choose the Question and the Smallest Useful Loop

Start from the decision: what should we do differently after seeing evidence?
Name the uncertain claim, what outcome matters, and what could refute it.
Distinguish intent and preference from factual assumptions with
[Align](../align/SKILL.md) when needed. A complete rubric is not a prerequisite
for looking at real examples.

For an unfamiliar problem, use a
[tracer bullet](../references/working-principles.md#tracer-bullets): one real
input through the relevant work to an observable outcome, inspectable trace,
and feedback. This establishes that the evaluation path can reveal useful
information before scaling it. Keep mocks, constructed examples, and missing
integrations explicit; they limit what the result establishes.

Scale effort to the consequence and uncertainty of the decision. Reuse valid
evidence. If further evaluation is unlikely to change the next action, stop
and state what remains unknown. Continuing work may need another checkpoint
when the environment, distribution, instructions, or evaluator changes.

When a result changes what AI or tools can reliably accomplish, feed it into
[Capability–Timescale](../capability-timescale/SKILL.md). It uses the evidence
to revise feasible scope, approach, resources, and pace. Distinguish production
capacity from verification capacity; an unreliable evaluator can itself limit
the automation we can responsibly trust. Use
[Define Goal](../define-goal/SKILL.md) when the next learning or delivery outcome
and its stopping condition need clarification.

## Separate Discovery from Measurement

**Discovery** asks what can go wrong, what "good" means in practice, and which
beliefs need revision. Inspect real attempts, including successes, failures,
ambiguous cases, and counterexamples. Agents can cluster and select evidence;
human observations can reveal criteria that were not known at the start.
Use provisional failure categories and revise them from examples.

**Measurement** asks how often something happens or whether a change helps.
Define the target population, sampling method, unit of analysis, denominator,
and criterion version. Complaint samples, judge disagreements, and deliberately
surprising cases are useful for discovery but do not directly estimate
production rates. Preserve a representative or random sample for such claims;
report selection limits when one is unavailable.

Do not quietly rewrite a rubric to make a candidate pass. When review changes
the meaning of quality, record the reason and version, regrade both versions
under a common standard where possible, or mark their scores incomparable.
An exploratory set becomes development data once used to tune the system or
judge. Keep fresh confirmation evidence for the resulting claim.

## Inspect What Actually Happened

Separate three kinds of evidence:

| Evidence | What it can establish | Common mistake |
|---|---|---|
| Outcome | The artifact's properties or a verified change in the environment | Treating "done" in a response as proof of an external effect |
| Process | Tool inputs and outputs, retrieved evidence, intermediate decisions, and failures | Enforcing an exact trajectory when multiple approaches are valid |
| Operational signals | Completion, tool errors, latency, tokens, cost | Treating clean execution or speed as correctness or usefulness |

Read traces to locate the first consequential divergence and inspect the
associated inputs, tool result, and final outcome. Verify source evidence;
judge explanations and agent summaries can be wrong. Missing evidence is
unknown, not an automatic pass. Distinguish a task failure from an invalid
evaluation attempt, and retain both rather than hiding inconvenient runs.

Prefer deterministic checks for objective properties. Use anchored human
judgment for contextual quality, and LLM judges when their reliability has
been established for the relevant criteria and cases. Combining methods is
often useful; a model's confidence or a majority of similar agents is not
independent validation.

## Use Human Attention to Resolve the Hard Part

Prepare a small review set around consequential uncertainty. Show the raw
case or contrast, the decision it informs, and any evidence needed to challenge
your interpretation. Preserve source access rather than giving only a verdict
or a long trace dump.

Read [human review](references/human-review.md) when arranging trace review,
learning the criteria with a human, or allocating a limited review budget.
It covers informative sampling, avoiding reviewer anchoring, and testing whether
understanding transfers to a new case. Let people mark uncertainty or revise
their judgment; human labels also need context and adjudication.

Read [judge calibration](references/judge-calibration.md) before relying on
LLM-as-a-judge results for a decision. Calibration is criterion-specific and
requires evidence outside the examples used to construct the judge. Overall
agreement can conceal failure to detect the cases that matter most.

## Compare Changes Fairly

For a system, prompt, skill, or workflow change:

1. Keep the baseline and candidate reproducible: record instruction versions,
   model/settings, tools, data, environment, and evaluator version. Record
   unavailable metadata rather than inventing equivalence.
2. Use the same relevant inputs with isolated state and comparable resources.
   Prevent candidate/baseline answer leakage, shared-memory effects, and
   contamination by expected answers. If a dependency cannot be fixed, report
   the confound instead of attributing the difference solely to the change.
3. Preserve per-case outputs and relevant traces. For stochastic behavior,
   choose repeated trials and uncertainty estimates appropriate to the claim.
   One successful attempt is evidence of possibility, not reliability.
4. Inspect differences, regressions, and grader errors, not just the average.
   Keep quality, operational cost, latency, and human review time separate.
   Use blind ordering when possible for subjective comparisons.
5. Confirm important improvements on cases excluded from tuning. Repeatedly
   checking a "test" set to select a winner makes it selection data; obtain
   new evidence before claiming held-out performance.

For delegated evaluation, separate execution, grading, and synthesis contexts
where that reduces leakage. Give executors raw inputs, not hidden answers or
the preferred conclusion. Give graders the criterion and source evidence.
Require evidence behind disagreements; adding agents with shared assumptions
does not add independent ground truth.

## Turn Findings into a Better Next Attempt

Report the decision supported, concrete evidence, uncertainty, and the next
intervention. Connect a failure to the layer that can address it: task framing,
inputs, tools, implementation, instructions, rubric, or evaluator. Re-run the
affected behavior and check relevant regressions after a change.

Also record which human or agent belief changed. When improved understanding
is part of the goal, test a later prediction or judgment on a fresh case and
track the review effort; do not claim learning from satisfaction or agreement.
An evaluation that finds an invalid premise or unreliable judge can be useful
even when it cannot yet rank systems.

Persist evidence and decisions when work will continue, with enough provenance
to reproduce or challenge the conclusion. No particular dashboard, registry,
runner, or trace format is required. Discussion, evaluation, and a favorable
result do not themselves authorize implementation, publication, or deployment.

## Domain Methods and Sources

Load only what the evaluation needs:

- Software correctness and failure paths: [testing](../../meiva-serious-engineering/practices/testing/SKILL.md) and [verification discipline](../../meiva-serious-engineering/ai-era/verification-discipline/SKILL.md).
- Research metrics and inferential uncertainty: [measure](../../meiva-serious-research/practices/measure/SKILL.md) and [verify](../../meiva-serious-research/practices/verify/SKILL.md).
- Meiva skill authoring, case formats, and replay evidence: [skill evaluation protocol](../../../platform/meiva-guide/references/evaluation.md).
- Method provenance and limits of the adaptations: [sources](references/sources.md).
