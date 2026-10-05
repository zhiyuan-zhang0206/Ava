# One universal skill index, and the four calls that shaped it

## Context

`skills_to_inject_into_system_prompt` shipped as a five-name whitelist
(`ava-fleet`, `ava-use-claude-code-and-codex`, `gmail`, `ava-ui`, `ava-watcher`).
Every other installed skill was invisible in the prompt: an agent learned it
existed only by calling `ava.help(ava.skills)`, which nothing in the prompt told
it to do. The observed failure was agents rebuilding, from general knowledge,
capabilities the cluster had already packaged — and being unable to notice,
because a capability you were never told about is indistinguishable from one that
does not exist.

The whitelist also forced a bad edit every time a skill was added: a new
top-level skill was dark until the config default changed on every cluster, which
is why `future/coding/default-skills.md` recommended nesting new packs under
an already-listed entry skill instead of promoting them.

## Decision

The default becomes `["*"]` — every loaded skill is indexed, name plus one-line
description, in the `# Capabilities` section. An explicit list on the field is
now a deliberate per-agent **narrowing**, not the baseline.

Because completeness alone does not make an agent read the index, the obligation
moved into the delegation check — the prompt's one mandatory-flagged process —
as its first step: match the task against `# Capabilities` before starting.

## Alternatives rejected

**Convert the seed presets' skill lists to `skills_to_expand_at_start` instead of
dropping them.** The five seeded presets carried per-role lists (a coder also got
`ava-code.worktree/pr/testing/conventions`). Read as additions on top of a 5-name
default they made sense; under a `*` default the identical value *shrinks* that
agent's index to nine names and hides everything else. Moving them to the preload
field would have preserved the roles' intent, but that field injects the FULL
SKILL.md body: a coder preset would have gone from four index lines to four whole
documents in every prompt, forever, as a side effect of a migration nobody read.
Silently multiplying a preset's prompt cost is not a migration's call. The lists
are dropped; role differentiation is left as its own piece of work, and the seed
presets ship with empty config in the meantime.

**A third attribution depth for "listed in the index".** `skill_invoked` rows
carry `invocation_depth`: `loaded` (the agent opened it) and `prompt_injected`
(the prompt listed it). With the index universal, `prompt_injected` now means
"installed on this machine" for nearly every skill — nearly no signal. The
tempting fix was a third depth separating "in the index" from "narrowed to this
agent". Rejected: `ava_self_evolution` scores on `loaded`, and the value of
`prompt_injected` was always the *contrast* with it, not its own information.
Adding a depth would add a column to every consumer's reasoning to recover a
distinction that is already derivable — the config list is on the agent's row.
The depths stay two, and `prompt_injected`'s reduced meaning is accepted.

**Redesign nested rendering to claw back the prompt growth.** The universal index
costs roughly +3.3k characters of system prompt on a fully-converged cluster (one
line per skill, nested paths included). A nested render — group by namespace,
one line per group, children indented or elided — would cut that. Rejected for
now: it trades a flat, greppable listing where every entry carries its own
`ava.skills.<path>` for a shape the agent has to reconstruct, and the failure it
guards against (rebuilding an existing capability) costs far more than 3.3k
characters. The growth is accepted as the price of completeness; if the catalog
outgrows it, the answer is a better render, not a shorter list.

**Filter capability-surface members only where they are rendered.**
`AVA_SDK_EXPAND` resolves `"*"` to concrete namespace paths. A path *inside* a
capability surface (`skills.gmail`) must never be expanded — resolving it walks
the same `getattr` an agent access takes, so it inlines that skill's whole body
into every prompt and records a `loaded` attribution nobody earned. The guard
originally sat in the render loop. Rejected in favour of refusing at
`effective_sdk_expand`, the resolution site: that view has other consumers (the
ava_code section reads it for its promote-vs-skip dedup) and its docstring
promises concrete paths only. A guard at one consumer is a guard the next
consumer does not get.

## Consequences

- A narrowed agent must still be able to find what it was not shown. The
  `# Capabilities` header says so explicitly — the listing may be a subset,
  `ava.help(ava.skills)` enumerates the full catalog, and an unlisted skill is
  still loadable by name. Narrowing is an attention decision, not a permission
  boundary, and every doc that framed it as one was corrected.
- Every installed skill's `description` is now in every prompt on the cluster, so
  a slack description is paid universally rather than by the few agents that
  listed it. The `skill-desc` sweeper class was recalibrated accordingly.
- Descriptions are free-form frontmatter from whoever wrote the SKILL.md,
  including drop-ins under `~/.ava/skills/`. With the whole catalog rendering,
  the index is now reachable by anything that can place a file there, so it is
  flattened and truncated at the render site.
- Agents carrying an orchestrator-authored `config_overlay` skill list keep a
  permanent narrowing. The migration cannot touch it: a deliberate narrowing and
  an inherited one are the same bytes. Those need a manual sweep.
- `skills_to_inject_into_system_prompt` is `lifecycle: "frozen"`, so this default
  change reaches agents born after it and nobody already alive — except that the
  birth-stamp backfill had just written the old 5-name list onto every live
  agent, which the migration therefore has to strip as well.
