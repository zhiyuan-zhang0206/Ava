# Skill Creator authoring evaluation

Read [the shared contract](../references/evaluation.md) before running these
cases. `evals.json` contains constructed, self-contained authoring scenarios.
All source data is inline in each prompt; there are no external fixture files.
Only writes under an isolated scratch directory are permitted. Live Ava
operations and external service calls are outside these cases' scope.

## Setup and baseline

Use the previous Skill Creator instruction package as the baseline and the
edited package as the candidate. Hold the executor model, effort, tools, and
scratch-directory layout fixed. Supply only the case prompt and relevant skill
instructions; keep expected outputs and assertions with the evaluator.
The tested creator may follow its reference links, but the evaluator's case
files and other run results should be inaccessible. Record any isolation gap.

Cases 1–3 cover objective authoring, discovery refinement, and subjective output.
Reserve case 4 for the final check; do not tune against its results. New cases
from real authoring failures should retain their provenance in private storage.

## Measures and acceptance

- **Case pass rate:** a case passes when every assertion in its record passes.
  Report passing / valid completed cases and execution coverage across all four.
  All four cases must pass for acceptance on this set; blocked or unexecuted
  cases leave acceptance incomplete.
- **Deliverable correctness:** inspect the generated skill, saved fixtures,
  cases, metrics, baseline snapshot or plan, and handover. Use deterministic
  checks for file contents, parseability, arithmetic, and preservation of the
  existing name/body; grade scope and rubric usefulness with cited evidence.
- **Evidence integrity:** any invented behavioral result, claim that format
  validation proves task quality, or unexecuted check recorded as passed fails
  its case. Missing runtime capability is an expected fixture condition, not a
  reason to penalize an honest draft handover.
- **Efficiency:** report elapsed time and input/output tokens per completed
  authoring attempt when the executor provides them. Cost is optional; identify
  its source or estimate basis. These are diagnostic measures, not a substitute
  for the acceptance checks.

The generated invoice skill's discovery metrics are a deliverable evaluated by
case 2. These authoring runs explicitly load Skill Creator and do not measure
Skill Creator's own implicit trigger accuracy.

## Results

Retain per-assertion evidence paths and the candidate/baseline comparison outside
the package. No behavioral run results are bundled here. Report what actually
ran, including failed, invalid, blocked, and unexecuted attempts. These offline
cases assess authoring decisions; they do not validate live registration or an
authored skill's eventual production task performance.
