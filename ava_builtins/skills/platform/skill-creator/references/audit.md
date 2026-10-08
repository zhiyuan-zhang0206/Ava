# Auditing an existing skill catalog

Use this guide for a catalog audit or a substantial change to discovery or
resource routing. Keep small self-contained skills small; a reference directory
is useful only when it separates material that a task can avoid loading.

## Discovery

Read the full package before shortening its description. State what the skill
does and when its instructions matter. Remove exhaustive tool lists, keyword
triggers, and claims broad enough to attract ordinary work. Distinguish adjacent
jobs with a concrete boundary, such as experiment design versus software design.
Keep the skill identity, optional metadata, and invocation policy unchanged
unless the user requests otherwise.

Shortness is a diagnostic. Measure description words or actual tokenizer output
with the unit named, then test selection with positive, negative, and neighboring
requests. A reduction in words does not demonstrate reduced runtime tokens,
latency, cost, or improved selection.

## Invocation and references

Keep purpose, mode decisions, essential constraints, completion criteria, and
useful routing in the entrypoint. Loading a skill does not assign a maintenance
role or authorize a rollout, schedule, message, or other external action.

Move substantial conditional procedures, worked examples, and source context
to focused references. State the read condition at the link. Do not make the
agent read every mode, and do not replace a useful short skill with an empty
router. Preserve fragile command sequences and operational invariants.

Before moving text, trace its consumers and snapshot the full package. Preserve
examples, scripts, assets, source attribution, and relative links. Command
examples retain their original execution directory unless explicitly changed;
the reference file's location is not the command's working directory.

## Evidence

Save cases, metrics, and the baseline choice using
[evaluation](../../ava-guide/references/evaluation.md). For catalog changes, record which skills and
behaviors each probe covers; a few selected cases do not validate the whole
catalog. Distinguish metadata-routing proxies, offline instruction-following
plans, actual harness selection, and live execution.

The portable [catalog cases](../evals/catalog-audit.json) and
[metric definitions](../evals/catalog-audit-metrics.md) cover selected routing
boundaries and operational navigation. They are evaluator inputs, not standing
instructions. Construct additional cases from real failures. Keep answer keys
and aggregate comparisons out of the executor's context; record invalid runs
and unavailable measurements rather than inventing evidence.

## Primary guidance

- [OpenAI Skill Creator](https://github.com/openai/skills/blob/main/skills/.system/skill-creator/SKILL.md):
  useful non-obvious instructions, precise discovery, and proportionate detail.
- [OpenAI on skills and prompts](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra):
  discriminating metadata and minimal routing among meaningful workflow modes.
- [Anthropic best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices):
  concise instructions, task-dependent references, and evaluations from usage.
- [OpenAI skill evaluations](https://developers.openai.com/blog/eval-skills) and
  [Anthropic Skill Creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md):
  observable outcomes, baselines, independent evidence, and iteration.

Checked 2026-10-08. Apply these principles to Ava's tools and repository limits;
another harness's metadata schema or size bound is not an Ava requirement.
