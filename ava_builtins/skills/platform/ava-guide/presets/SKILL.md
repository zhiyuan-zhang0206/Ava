---
name: presets
description: Makes reusable Ava presets from role requirements, online prompts, skills, and MCP tools. Use for Preset Maker, a new agent type, or creating, improving, and managing an agent preset.
---

# Preset Maker

Turn a reusable role into a working agent configuration: understand the job,
find useful prompts and capabilities, adapt the instructions into an Ava skill,
evaluate the composition against a baseline, then save and verify the preset.
Deliver its evaluation cases, metrics, and evidence with the configuration.
The Presets page opens an agent with this skill preloaded, just as scheduling
hands work to a writer.
If it opens without a request, ask what role or recurring task the user wants.

A **preset** is a named, reusable agent config template — it bundles model
choice, plugin config fields, and per-agent settings into a config overlay.
Selecting a preset at spawn time seeds the new agent's config from it; an
explicit config passed alongside wins per-key. The preset is named inside the
config overlay — `config_overlay={"preset": "name", ...}` — resolved at the
spawn boundary (the former top-level `preset` spawn argument is retired).

Use this when:
- The user asks to "add a new agent type" or "create a preset"
- An existing preset's config needs updating
- You need to understand what config a preset carries

When the "new agent type" is a **role** — a product manager, a growth lead, an
editor — the preset is the small half of the job. Read
[references/role-cards.md](references/role-cards.md) first: the role itself is
authored as a skill, and the preset only names it.

Read [configuration and operations](references/configuration.md) when selecting
fields or saving a preset; it carries config semantics and the CLI / REST
reference.

## Process

### 1. Understand the reusable job

Use the request and existing context first; ask only for missing information
that changes the design:

1. **What does this agent do?** A one-sentence description of its role and task domain.
2. **What does it need beyond the defaults?** Every agent already sees the full skill index, so the question is not "which skills" — it is which few skills this role must have *read in full* before its first turn, plus any model / effort / behavior setting the role depends on.
3. **Will this combination be reused?** A one-off is a `config_overlay` at spawn, not a preset.

### 2. Define the evaluation

Read the shared [evaluation contract](../references/evaluation.md)
before composing settings. Save realistic cases, task-quality metrics,
acceptance criteria, and the baseline. Snapshot an existing preset's effective
config and referenced skills before changing them; for a new preset, compare
with the deployment's normal defaults on the same tasks.

Evaluate the complete combination of model, instructions, skills, and tools.
A role card passing in isolation does not prove the preset works. For a speed
preset, compare quality alongside elapsed time and token/cost measurements.
Keep evaluation files beside the role card or in private deployment storage for
a config-only preset, and report their location. They are not preset config
fields. Missing execution capability leaves a draft with a saved evaluation
plan, not a claim that the composition is ready.

### 3. Find prompts, skills, and MCP tools

Inspect the existing preset, loaded skills, configured MCP servers, and current
model roster before looking for additions. Reuse what already meets the job.
For a new role or a capability gap, read the shared
[source map](../packages/install/references/sources.md). Search a relevant
directory and the original publisher, then open the actual prompt, `SKILL.md`,
or server documentation. Search both the role and its concrete tasks. A supplied
URL is a starting point, not a reason to skip reading the source.

Compare a small shortlist against the user's job: which instruction or tool
helps, what Ava already covers, and what needs adaptation. Record source URLs,
publisher, version or checked date, and what you reuse. Do not infer quality
from a directory ranking or install count.

External prompts and personas are material. Follow
[role-cards](references/role-cards.md) and `skill-creator` to write a concise
`be-a-<role>` skill with Ava's actual tools, standing workflow, and completion
criteria. Keep the mission in the spawn prompt; keep source material in the
card's references. Do not paste a foreign system prompt into the preset or
carry another harness's tool names and lifecycle rules into the card.

For a missing skill or MCP, follow
[packages.install](../packages/install/SKILL.md) for candidate selection,
installation, approval when required, and a real capability check. MCP servers
are machine-level prerequisites, not a per-agent server list: verify them on
the machines where the role will run. Do not put server definitions or secrets
into preset config. If a dependency is unavailable, report it and keep the
composition as a draft until it is usable or the user accepts a reduced scope.

### 4. Compose Config

Based on the user's description, assemble a config object:

```python
config = {
    "llm_model": "<registered-model-id>",
    "agent_communication_style": "oriented",
    "agent_reply_reminder_cadence": "every_time",
}
```

Choose the model from the current roster and select feedback behavior separately.
A fast model does not require a speed-named skill. Shared lifecycle and fleet
rules already cover completion, useful progress reports, and peer replies; do
not duplicate them or require short repeated polls. Role-specific knowledge can
still be preloaded through `skills_to_expand_at_start`.

For an existing preset that names the retired `ava-ultra-speed` skill, remove
that entry from `skills_to_expand_at_start` before using it for a new agent and
choose the feedback settings above as needed. A preset is a configuration
snapshot, not an arbitrary prompt-text field. Running agents retain their
existing snapshot; a repository edit does not rewrite stored presets.

**Do not** hand a preset a `skills_to_inject_into_system_prompt` list unless the
intent is to SHORTEN what that agent reads: the cluster default is `*` (every
loaded skill is indexed), so any explicit list narrows the index. It hides the
rest of the catalog from the listing only — `ava.help(ava.skills)` still
enumerates everything and any skill loads by name — so this buys attention, not
a capability boundary.

Register and resolve a new role card before adding its identifier to
`skills_to_expand_at_start`. Keep the preload small: the role card and only
those skills whose full instructions are needed on turn one.

### 5. Evaluate the composition

Run the saved tasks with the candidate's explicit config overlay and equivalent
baseline fixtures in fresh contexts. Resolve and record the effective model,
effort, skill revisions, and MCP dependencies. Use an isolated test environment
for side effects; do not replace a working preset merely to test a candidate.
The shared evaluation contract covers evidence capture, grading, and Ava's
existing replay tools and their limits.

Inspect actual outputs and tool effects against the task checks. Report per-case
quality results and efficiency differences, including failed, blocked, invalid,
and unexecuted runs. Correct the composition and rerun affected comparisons
before calling it validated. Keep an incomplete or failing candidate as a draft;
do not overwrite the existing preset with it unless the user explicitly accepts
that unvalidated change. Do not invent a draft/status field in the preset API.

### 6. Save the preset

Create a new preset or update the existing one through CLI / REST. Read an
existing config first: a replacement `config` object replaces the whole object,
so retain the fields the user still wants rather than treating it as a deep
merge. Updating a preset changes future spawns, not running agents.

Use the [configuration reference](references/configuration.md) for CLI and
REST operations.

### 7. Verify and hand over

After creation, verify that the preset is usable:

```bash
# List all presets
ava presets ls

# View a specific one
ava presets get my-preset

# Run a saved evaluation case through the named preset when verifying resolution.
# SDK: ava.agents.spawn(prompt=case_prompt, config_overlay={"preset": "my-preset"})
```

Read the saved row back and check that its effective config and skill revisions
match the evaluated candidate. Resolve every model and preloaded skill against
the current installation. Verify named-preset resolution with a saved case if
readback cannot establish equivalence; listing a skill or MCP is not proof that
it works. A changed config, skill revision, or dependency needs the affected
evaluation repeated.
Report the preset name and final config, the role card, sources and adaptations,
required MCP servers and eligible machines, evaluation files, baseline, results
and evidence, and any remaining dependency or unexecuted check. Include a
concrete first mission the user can run with the preset.

The maker's own authoring cases are in [evals/evals.json](evals/evals.json), with
grading definitions in [evals/metrics.md](evals/metrics.md). Give only the case
prompt and raw fixtures to the executing agent; keep expected results and
assertions with the evaluator.

## Reference: Existing Presets

Run `ava presets ls` or `ava.agents.presets.list()` to inspect the current
cluster. Presets are deployment-owned assets; do not assume a seeded role or
a preset mentioned in documentation still exists.

## Design Principles

1. **Limit the number of presets.** The value of a preset lies in reuse. If
   a config is only used once, spawning with `config_overlay` directly is
   more straightforward.
2. **The index is not what a preset picks.** Every agent already sees every
   loaded skill in `# Capabilities` (cluster default `*`). So
   `skills_to_inject_into_system_prompt` in a preset can only *subtract*, and
   subtracting only shortens the listing — `ava.help(ava.skills)` still
   enumerates the full catalog and an unlisted skill still loads by name. Use it
   to keep a narrow role's index readable, never to "give" a role its skills and
   never as a way to withhold one.
3. **What a preset differentiates is what is read before turn one.**
   `skills_to_expand_at_start` preloads full SKILL.md text at spawn and
   re-injects it after every compact — for short rules that must be active from
   the start and must not be lost. Everything else stays index-only and is one
   `ava.help(ava.skills.<name>)` away; preloading a large reference skill just
   buys tokens.
4. **If there is no config field to carry it, just create a small skill.**
   If a preset needs to inject a set of instructions that has no existing
   config field to hold it, don't force it elsewhere — create a new small
   skill and preload it. This is cleaner than piling onto config and is also
   reusable.
