---
name: align
description: "Aligns intent and calibrates human and agent understanding against evidence. Use when an unresolved preference, assumption, or knowledge gap changes the work."
---

# Align — Intent, Understanding, and Reality

Align has three responsibilities:

| Relationship | What can need correction | What helps |
|---|---|---|
| Agent and human intent | A misunderstood purpose, preference, trade-off, or authority boundary | Existing instructions, a concrete choice, or a brief clarification |
| Human understanding and reality | An incomplete or mistaken model of the problem or system | Inspectable observations, contrasts, counterexamples, and experience |
| Agent understanding and reality | An unsupported assumption, stale knowledge, or mistaken interpretation | Direct investigation, independent evidence, and testing a prediction |

Calibration is part of Align throughout the work. There is no separate
Calibrate stage to finish first. The objective is enough shared understanding
to choose the next useful action, with uncertainty visible.

Human preferences and permission are authoritative within the task; factual
beliefs are claims to investigate. Evidence can challenge "this cannot be done
locally" without overruling "these records must stay on this device." Human agreement
and agreement among agents do not establish a fact.

## Start from the Actual Gap

Reuse the conversation, existing decisions, and available evidence. Briefly
identify the desired outcome, the belief or choice that could change the next
action, and whose understanding is uncertain. Do not assume the human lacks
knowledge, or that an unfamiliar topic makes the agent correct.

Distinguish observations, interpretations, preferences, and unknowns. For a
consequential assumption, state the current prediction and what observation
would change it. Label inferred beliefs as tentative; let the human correct
your interpretation without requiring a survey of everything they know.

Investigate discoverable facts before outsourcing that work to the human.
Ask human-only choices directly. When a question is needed, explain what
depends on it and offer a supported recommendation. Present a real trade-off,
not a leading question designed to obtain agreement. Existing authorization
persists; a written alignment note creates no new approval gate.

## Spend Attention Where Understanding Can Change

Agents can search, compare, replay, and group far more material than the human
can review. Use that capacity to prepare a small amount of high-value evidence:

- A consequential counterexample to a current belief.
- A contrasting pair that makes a hidden distinction visible.
- A disagreement between outcomes, a metric, and a judge's explanation.
- An unfamiliar dependency whose failure would change the plan.

Prioritize expected decision value per unit of human attention: consequence,
uncertainty, and whether seeing the evidence could change an action. Surprise
is useful when it corrects a material belief; novelty alone is not a reason to
interrupt. Include ordinary cases when needed to convey context. A curated
surprise set does not estimate how common a problem is.

Show the concrete artifact or relevant trace segment with access to its source.
Separate what happened from what you infer. Ask for a judgment or prediction
only when it would reveal a useful distinction. Give the human room to notice
something the agent missed; record corrections to the agent's model as well
as changes in the human's understanding.

Use [Evaluate](../evaluate/SKILL.md) when evidence selection, grading, trace
review, or evaluator reliability needs deliberate work. Its
[human review guidance](../evaluate/references/human-review.md) describes how
to keep preparation cheap for the human without hiding uncertainty.

## Learn Through a Real Path

When discussion cannot settle a material uncertainty, propose or run an
authorized [tracer bullet](../references/working-principles.md#tracer-bullets):
one small path through actual work to an observable outcome and feedback.
Choose what it needs to teach before expanding its breadth.

For example, a teaching pilot can go from one lesson to a learner's attempt
at a new problem. Their errors may change the human's belief about prerequisite
knowledge, the agent's understanding of the learning objective, and the next
lesson. A polished lesson outline alone cannot establish learning. Reuse the
working path while revising it from evidence.

A request to discuss a pilot authorizes its design, not contacting participants
or building it. Continue independent work while waiting for genuinely missing
authority or preferences.

## Keep a Small, Revisable Shared Model

For substantial or continuing work, retain only what the next decision needs:

- Purpose, relevant preferences, boundaries, and settled decisions.
- Material beliefs, supporting observations, and remaining uncertainty.
- What changed in either participant's understanding and why.
- The next action, its expected evidence, and what would make us reconsider.

A few sentences may suffice. Do not require every branch of a design tree to
be closed. Continue when the next action is justified and authorized; unresolved
questions can remain explicit. Reopen alignment when evidence challenges the
goal, assumptions, or criteria, without reopening settled consent.

Use [Capability–Timescale](../capability-timescale/SKILL.md) when the shared
model depends on what AI or tools can do now, at what quality, or at what pace.
Its evidence can expand feasible choices as well as expose limits; calibrate
both participants' expectations before rejecting or committing to an approach.
Use [Define Goal](../define-goal/SKILL.md) if the resulting intent still lacks
a recognizable outcome or stopping condition.

Evaluate tests whether the observations support a belief or result. Align
uses those observations to revise understanding and choices. Neither is a
linear gate, and neither treats a more confident human as proof of better
understanding. Where learning matters, a later prediction or judgment on a
fresh case is stronger evidence than "I understand."
