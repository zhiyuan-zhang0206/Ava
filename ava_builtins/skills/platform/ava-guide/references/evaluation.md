# Evaluating skills and presets

Read this when defining or running an evaluation for a new skill, changed
instructions, trigger description, or preset composition. `skill-creator` owns
this authoring evaluation contract; Preset Maker uses it for the complete
configuration. The canonical file is bundled with Ava Guide so its external
Codex and Claude Code copies include every reading dependency. Skill Creator
links here rather than maintaining a second copy. This is an instruction-level
workflow, not a new runtime API or automatic evaluation service.

## Save the evaluation before running it

Every new skill or preset, and every behavioral improvement, carries:

- **Cases:** realistic prompts, input fixtures, relevant environment, expected
  outcomes, and observable checks. Prefer actual user tasks; mark constructed
  cases as constructed. Include important boundary and failure paths.
- **Metrics:** each measure's meaning, evidence source, grading method, and
  acceptance criterion. Include a task-quality measure. Add efficiency measures
  where speed or cost is part of the purpose.
- **Baseline:** the version or configuration to compare with, and what variable
  changes. Snapshot it before editing.
- **Results:** case-level grades with evidence, run metadata, aggregate
  comparison, feedback, and execution gaps.

Start with a few cases that cover distinct decisions and expand from observed
failures. Reserve cases outside the tuning loop for substantial changes. A
small set demonstrates those cases, not a population-wide quality guarantee.

For a skill, keep portable inputs in `evals/evals.json` and metric definitions
in `evals/metrics.md`. Put sensitive deployment cases in private storage and
record their location. For a preset with a role card, its evaluation can live
beside that card. For a config-only preset, keep a separate evaluation directory
in the maker's workspace or private deployment storage. Never invent evaluation
fields in the preset's config: its API carries configuration, not test data.
Record the evaluation location in the handover.

### Case record

The creator and Preset Maker bundle their own case sets in this shape. It is a
file convention for evaluators, not input accepted by Ava's replay harness.

```json
{
  "skill_name": "invoice-checker",
  "evals": [
    {
      "id": 1,
      "prompt": "Validate invoices.csv before import and report incorrect totals.",
      "files": ["fixtures/invoices.csv"],
      "expected_output": "Report invoice A-17: recorded total 120, calculated total 100.",
      "assertions": [
        "Identifies A-17 and both totals from the input.",
        "Leaves the input file unchanged."
      ]
    }
  ]
}
```

Save the referenced fixture, not just its filename. For each set, record fixture
provenance, environment and dependencies, allowed side effects, and any required
credentials without storing secrets. Checks and acceptance criteria should be
fixed before grading. If a check proves wrong, document the correction and
regrade both versions; do not silently move the target to fit the candidate.

## Choose a meaningful baseline

| Change | Baseline | Candidate |
|---|---|---|
| New skill | Same task without the added skill | Same task with the skill available or explicitly loaded |
| Existing skill | Snapshot of the previous instruction package | Edited package |
| New preset | Same task under the normal default configuration | Proposed effective configuration |
| Existing preset | Snapshot of its previous effective config and referenced skills | Proposed config and skill revisions |

For skill execution tests, explicitly load the skill to measure its workflow.
For discovery tests, make it available in the ordinary index without preloading
or naming it in the prompt. A preloaded role card tests behavior, not triggering.
For a no-skill baseline, remove the added skill from the available catalog; do
not merely omit its path while leaving it discoverable. Isolate old and new
versions so registry resolution cannot select the wrong one.

Resolve a preset's effective config and referenced skill revisions before the
run. If the preset or role card changes midway, it is a different candidate;
do not aggregate it into the same result. Comparing the whole preset shows the
combination's value; attributing that value to one skill requires a separate
controlled comparison.

## Define metrics for the actual job

| Measure | Definition and evidence |
|---|---|
| Case pass rate | Cases meeting every declared acceptance check / valid completed cases; show the numerator and denominator. |
| Check pass rate | Passed applicable assertions / evaluated applicable assertions, with per-check evidence. Keep critical failures visible. |
| Quality rubric | Named dimensions with anchored scores, such as unsupported / partly supported / fully supported claims. Attach the assessed artifact and grading rationale. |
| Trigger recall | Correct activations / prompts that should activate the skill. Use implicit-discovery cases. |
| False-trigger rate | Incorrect activations / prompts that should not activate the skill. Include adjacent jobs. |
| Elapsed time | End-to-end wall time per completed attempt, captured from the runner; state what waiting or setup it includes. |
| Tokens and cost | Recorded input/output tokens and actual or explicitly estimated cost, with model, pricing basis, and unavailable fields named. |

Choose only measures that answer the job's question. Keep quality and efficiency
separate; quick, incorrect output is not a success. Define thresholds or the
acceptable comparison before execution rather than inventing a universal score.
For subjective tasks, use an anchored rubric and examples or a blind human
comparison. Do not turn stylistic taste into arbitrary exact-string assertions.

Candidate and baseline must see the same prompt, input fixtures, model and effort
unless those are the variables under test. Record tool and MCP versions, host,
dependency readiness, context limits, and relevant config. Use fresh contexts
and reset writable fixtures between attempts. Run order and live service
conditions can affect time measurements; repeat noisy cases and report the
number of attempts and variation before claiming an improvement.

## Execute and capture evidence

Keep results outside the skill package, with a run directory per case and
configuration. Each attempt records:

- Case ID, candidate/baseline revision and effective config, environment, and
  start/end times.
- Agent or runner identity, trace location, resulting artifacts, and observed
  token/time/cost values. Unknown values remain unavailable, not zero.
- Each check's pass/fail or rubric grade and evidence location, plus human
  feedback when used.
- Whether it completed, failed, was blocked, was not run, or was invalidated,
  with the reason.

Give the executing agent only the task and raw inputs it would normally receive.
Keep expected outputs, assertions, baseline results, and grading notes with the
evaluator. Do not preload `evals/` or the answer-bearing part of this reference
into the tested role. Fresh context alone does not isolate shared memory,
filesystems, DB access, or connected services; record what was actually isolated.

Use deterministic checks for calculations, schemas, file changes, and tool
effects. A tool call alone may not prove its outcome: inspect the artifact or
read back the effect. Use a separate rubric pass for qualities rules cannot
establish. Blind the evaluator to candidate labels when feasible and review
its evidence rather than accepting a score without rationale.

Use disposable fixtures or test services for writing tasks. Replaying an old
request does not authorize production preset changes, messages, installations,
or MCP writes. Preserve the user's scope and the installation/operation owner's
authorization requirements.

### Existing Ava evaluation tools

For dataset-derived, replay-safe read/compute tasks in an Ava deployment, load
`ava.skills.ava_self_evolution.evaluation` using `ava.help` and follow that
skill's `scripts/evaluate.py` launch/poll/gather flow. This separate built-in
is available through Ava's capability index, not bundled with an external
Guide copy; confirm its availability before choosing replay. It requires dataset trace
records and a live agent identity for launch; it does not ingest the case JSON
above, install a candidate skill snapshot, or resolve a preset comparison for
you. Its completion and efficiency scores are proxies, so also grade the
task-specific checks. Respect its replay and leak-audit gates and the documented
limits of isolation.

For authored cases or side-effecting workflows that this harness cannot replay,
use the available execution surface with explicit test fixtures and equivalent
baseline setup. If no suitable isolation or runner is available, deliver the
cases and metrics with an incomplete evaluation report. Do not bypass a gate or
claim a ready preset from a successful registration or dependency listing.

## Compare and report

Show candidate and baseline side by side, with per-case failures and quality,
time, token, and cost differences. Use only matched valid attempts for paired
comparisons. Report completed failures, blocked/unexecuted attempts, and invalid
runs separately; include execution coverage across the planned set so omissions
cannot make a low-coverage pass rate look like full success. An empty valid set
has no pass rate.

Inspect traces for checks that pass in both versions without discriminating,
leaked answers, and gains caused by changed tools or conditions. Make a
targeted correction, rerun affected cases and their baselines, then check the
reserved cases. Quality acceptance takes precedence over a favorable efficiency
number. Incomplete or failing evaluation remains a draft with the gap named.

## Official methods used

Checked 2026-10-08. These sources inform the method; their harness-specific
scripts and tool names are not Ava interfaces.

- [Anthropic Skill Creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)
  and [its evaluation update](https://claude.com/resources/articles/improving-skill-creator-test-measure-and-refine-agent-skills):
  saved cases, baseline comparisons, outcome review, and efficiency measurement.
- [OpenAI skill evaluation guidance](https://developers.openai.com/blog/eval-skills):
  success criteria, trace-based checks, and structured quality grading.
- [OpenAI's current authoring guidance](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)
  and [Codex Skill Creator](https://github.com/openai/codex/blob/main/codex-rs/skills/src/assets/samples/skill-creator/SKILL.md):
  precise discovery, progressive disclosure, and guidance proportional to the job.
