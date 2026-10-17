# Human Review with Limited Attention

Use this guide when the human needs to understand unfamiliar behavior, discover
criteria, or resolve cases that automated checks cannot judge reliably.

## Prepare the Next Decision

Infer the available attention from the request; ask about a review budget only
if it materially changes the approach. Identify the consequential uncertainty
and the belief or choice the next case could change. Do preparatory search,
replay, grouping, and source checking within the authorized resources.

Choose cases for a reason: an unexpected outcome, an important disagreement,
a high-cost error, a boundary between interpretations, or an ordinary contrast
needed to understand one of these. Avoid a feed of ever more dramatic failures.
Group similar cases to reduce duplicate reading, but inspect whether grouping
has hidden a distinct failure. Agents' confidence is not a substitute for
checking the original case.

Keep a separate random or representative stream when the question concerns
frequency, regressions outside known clusters, or silent failures. Record why
each stream was selected and do not pool them into a prevalence estimate
without a valid sampling analysis. More agent-generated cases improve coverage
of hypotheses, not necessarily coverage of the real population.

## Make Each Review Worthwhile

Present one case or a small contrast with:

- The original task and enough context to interpret it.
- The relevant artifact, tool result, or trace segment.
- The judgment or decision the human can help make.
- A path to the full evidence and any missing context.

Collapse routine trace detail, but retain the causal chain and contradictory
evidence. Separate source material from the agent's interpretation. A summary
should reduce navigation, not remove the ability to disagree.

For label collection or a subjective comparison, let the human judge before
revealing model scores, favored versions, or explanatory labels where practical.
Use blind ordering when it helps. For teaching, an explanation or contrast may
be useful immediately; do not treat that influenced judgment as an independent
label for judge calibration.

Begin with observations such as "what is wrong, and where did it first matter?"
when failure categories are not yet understood. Let the human express free-text
notes, uncertainty, and competing interpretations. Use those observations to
propose categories and anchored criteria. Return important boundary cases for
adjudication instead of turning an agent's clustering into ground truth.

Prefer an existing artifact viewer or short evidence packet. Build a review
interface only when repeated navigation is the bottleneck and implementation
is authorized; evaluation does not require a dashboard.

## Close Both Feedback Loops

After a useful review, capture the evidence, corrected belief, affected
criterion, and next action. Changes can affect the human's mental model, the
agent's model, the system, or the evaluator. A disagreement may reveal an
ambiguous goal rather than a defective answer.

Use the revised understanding to choose another informative case or an
intervention. Stop when the next decision is adequately supported, or when the
remaining uncertainty needs new evidence rather than more discussion.

When improving human understanding is an explicit goal, a brief prediction
before seeing an outcome, followed later by a judgment on a fresh case, can
test transfer. Respect the person's time; do not quiz them for every task.
Track review time and whether understanding changed a decision or generalized.
Do not substitute confidence, satisfaction, agreement with the agent, or number
of cases reviewed for demonstrated learning.

The agent also needs a transfer check: use the human's correction on a new case
and expose uncertain interpretations. Preserve unresolved disagreement rather
than claiming everyone has aligned.
