---
name: skill-creator
description: Creates, improves, and evaluates Ava skills. Use when authoring a skill, changing its instructions, tuning its trigger description, or evaluating skill performance.
---

# Skill Creator

Create reusable guidance that improves Ava's work on a specific job. Deliver
the skill together with evaluation cases, metrics, and evidence. Adapt the
authoring and evaluation methods of Anthropic and OpenAI to Ava's actual tools;
the [evaluation reference](references/evaluation.md) records the sources.

## What belongs in a skill

A skill carries a repeatable workflow, domain knowledge, or a standing
preference that changes the agent's decisions. Keep information the model needs
for this job; remove generic advice and instructions already owned elsewhere.
Use a plugin when the extension needs runtime state or hooks.

Ava reads the [Agent Skills](https://agentskills.io) format. Required frontmatter
is `name` and `description`; optional fields such as `license`, `compatibility`,
`metadata`, and `allowed-tools` are preserved but not enforced. Format
compatibility does not make another harness's tools or lifecycle portable.
Check those assumptions before using external material. Ava's single tool is
`execute_code`, with capabilities under `ava.*`.

```text
skill-name/
├── SKILL.md          # name, description, and essential instructions
├── references/      # substantial guidance read when needed
├── scripts/         # reusable executable operations, when justified
├── assets/          # resources used in the output, when justified
└── evals/           # cases and metric definitions
```

Only `SKILL.md` is required by the format. Create the supporting directories
that the job needs. Keep run artifacts outside the instruction package.

| Load level | Content | Authoring rule |
|---|---|---|
| Discovery | Name and description | State the capability and precise situations where it applies. |
| Invocation | SKILL.md body | Keep essential decisions, constraints, and resource routing here. |
| On demand | References, scripts, assets | Link each resource where it becomes useful; avoid duplicate instructions. |

The description guides implicit selection. Explicit invocation and preset
preloading also load skills, so test discovery separately from execution.

For a catalog audit or substantial discovery/resource-routing change, read
[audit guidance](references/audit.md) for criteria and focused evaluation cases.

## Authoring flow

### 1. Understand the job and define success

Extract the goal, expected output, trigger situations, and constraints from the
request and existing context. Ask only for missing information that changes the
design. Read relevant examples, dependencies, and current tool documentation.

Before drafting, define observable success and acceptance criteria. A subjective
job still needs a case and an anchored quality rubric; it does not need a fake
binary answer key. For every new skill or behavior change, read
[evaluation](references/evaluation.md) and save the case set, metrics, and
baseline choice. A wording-only correction can retain the existing plan and
verify the affected behavior.

### 2. Draft focused instructions

Keep the original name when improving an existing skill. Snapshot its full
instruction package and evaluation plan before editing so the old version can
serve as the baseline.

Write a short, discriminating description. Add a boundary when it prevents
likely confusion with an adjacent job. Do not broaden the trigger just to
increase invocation frequency.

```yaml
---
name: invoice-checker
description: Checks invoice CSV totals and missing required fields. Use when validating invoice data before import.
---
```

Give the model the job's useful decisions, actual tools, completion criteria,
and expected output. Explain non-obvious constraints. Leave room for judgment
when several approaches work; reserve fixed sequences for fragile operations.
Keep one-off missions and source material out of standing instructions.

Use progressive disclosure before the body becomes unwieldy; an upper size
bound is not a target. Add a script when repeated code or a deterministic
operation earns one; run it to verify its behavior. Substantial conditional
guidance belongs in references with clear read conditions.

### 3. Run the cases and baseline

Start with a small, realistic set that covers the affected behavior; add cases
from actual failures. Use fresh contexts and equivalent fixtures for candidate
and baseline. Keep model, tools, and environment fixed when measuring an
instruction change. When comparing models, record that model choice is the
changed variable.

Use the available agent execution surface within the user's authorized scope.
The [evaluation reference](references/evaluation.md) explains evidence capture,
grading, trigger tests, and Ava's existing replay tools. It does not provide a
new automatic runner. If execution or isolation is unavailable, save the plan
and report the missing check; do not claim a behavioral pass from a file lint.

### 4. Grade, compare, and improve

Grade outputs and traces against the saved checks. Use deterministic checks for
verifiable facts and an anchored rubric or human review for quality. Preserve
per-case evidence and feedback alongside the aggregate comparison.

Resolve failed acceptance checks before declaring the skill validated. Compare
quality separately from elapsed time, token use, and cost. Inspect both versions'
traces when a score changes; a high pass rate with no baseline advantage may
mean the skill adds no value or the cases do not distinguish the versions.

Improve the general rule supported by failures rather than adding instructions
for each example. Re-run affected cases after changes. Keep some cases out of
the tuning loop and use them for a final check when making a substantial change.

### 5. Hand over the skill and evaluation

Report the skill path and revision, case and metric files, baseline, per-case
results, comparison, artifact locations, and remaining limitations. Mark
unexecuted, blocked, and invalid runs explicitly. Preserve the candidate as a
draft when evaluation is incomplete; registration is not proof of quality.

For external material, retain source URLs, publisher, version or checked date,
and adaptations. Follow the installation guide for package mechanics; this
authoring flow does not expand installation or external-action authorization.

## Review checklist

- **Discovery:** Does the description select the intended job and avoid nearby
  jobs? Were positive and negative discovery cases checked when it changed?
- **Instructions:** Does each instruction change a useful decision? Are scope,
  completion, resource routing, and actual Ava tools clear?
- **Resources:** Are scripts exercised and references reachable? Is source
  material distinguished from standing instructions?
- **Evaluation:** Are cases, metric definitions, acceptance criteria, and a
  baseline saved? Are results supported by outputs and traces, with gaps named?
- **Regression:** Does the change generalize beyond the tuning examples? Is any
  speed or cost improvement accompanied by acceptable task quality?

The creator's own authoring cases live in [evals/evals.json](evals/evals.json),
with grading definitions in [evals/metrics.md](evals/metrics.md). Use them when changing this workflow; their prompts are inputs, and their
expected outputs and assertions belong to the evaluator.
