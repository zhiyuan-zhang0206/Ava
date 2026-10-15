# Workflow instruction evaluation

These cases assess instruction-following on bounded constructed tasks. They do
not measure production quality, human learning, or a calibrated LLM judge.

## Protocol

Compare a snapshot of the previous full Workflow package with the candidate.
Give each executor only its package, one case prompt, and copies of that case's
raw fixtures. Keep this file, expected outputs, assertions, the other version,
and other attempts' results with the evaluator. Use fresh contexts per attempt;
record model, available tools, package revision or hash, and output locations.
Fresh context is not filesystem isolation: record whether access restrictions
are enforced or instruction-only.

Cases 1-3 are development cases. Reserve case 4 until the candidate is frozen.
If it informs an edit, mark it development and obtain a new confirmation case.
Run the same cases for both versions. Grade failures and unavailable evidence
explicitly; do not drop inconvenient attempts. One attempt per case is a smoke
check, not a variance estimate or evidence of general improvement.

## Measures and acceptance

- **Case acceptance:** every saved assertion passes with an output/tool-evidence
  location. Report counts per case and per configuration, including failures.
  Missing evidence is unknown, not a pass.
- **Review usefulness:** human review of whether the response selects a concrete
  observation, explains which belief or decision it tests, and proposes a
  practical next action within the attention budget. Agent grading can prepare
  this judgment but cannot substitute for the user's review.
- **Scope and proportionality:** prohibited side effects or fabricated evidence
  fail the case regardless of other checks. Routine arithmetic should remain
  routine.

Do not claim quality improvement from tied scores or higher verbosity. Record
duration, tokens, and cost only when supplied by the execution surface; unknown
values remain unavailable. The protocol defines no efficiency threshold.

## Discovery checks

Check selection from descriptions separately from execution: understanding a
workflow or questioning its premises should select Align; diagnosing output
quality or validating a judge should select Evaluate; a known CSV sum should
use a direct check. Record manual routing inspection as inspection, not an
observed invocation result.

## Follow-up with the human

After the user reviews the skill, exercise it on an authorized real task. Track
review time, important failures found, decisions changed, and whether the human
and agents can apply the revised understanding to a new case. Do not infer those
outcomes from these authored-case responses or from the user simply saying the
explanation was clear.
