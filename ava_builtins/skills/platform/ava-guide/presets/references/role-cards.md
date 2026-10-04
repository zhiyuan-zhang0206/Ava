# Authoring a role card

A preset carries config. A role — "you are a product manager", "you run growth"
— is not config; it is instructions, and the thing that carries instructions in
this system is a skill. So a role is a skill: a **role card**. The preset's
whole job for a role is to name that card in `skills_to_expand_at_start`.

`skill-creator` covers how to write a skill at all — frontmatter, voice, when to
split into resources. Read it first. This page is only what is different about a
role.

## Three pieces, three lifetimes

| Piece | Carries | Where it lives |
|---|---|---|
| Role card (a skill) | who this agent is — standing, identical on every spawn | instance layer, on disk |
| Preset | the card's name in `skills_to_expand_at_start`, plus model / effort | cluster DB |
| Spawn prompt | the mission — what THIS agent is doing right now | the spawn call |

Keeping them apart is the whole design. A mission baked into the card makes the
card single-use; a role re-typed into every spawn prompt drifts between spawns.

## Name it `be-a-<role>`

`be-a-product-manager`, `be-a-growth-lead`. Dash form, flat at the top level —
not nested under a parent skill, because a role is not a sub-topic of anything.
The prefix is the whole namespace: it groups role cards in an index that lists
every loaded skill, and it reads correctly at the one place the name is typed,
`skills_to_expand_at_start: ["be-a-product-manager"]`.

## Author it into the instance layer

Write the card under this cluster's skill directory — `$AVA_HOME/skills/be-a-<role>/`
— then hand it to the registry:

```bash
ava skill register be-a-<role>
```

A hand-written directory sits there invisible until it is registered; after
that it is a `user` package, which is what keeps converge from ever touching it
and what keeps the card out of the repo's upgrade path. It goes through the
same supply-chain scan as anything installed, and it is live on the next skill
scan — no restart.

## An external prompt is material, not the body

The usual trigger for this job is a good `<role>` system prompt found somewhere
— a repo, a blog post, another product. Do not paste it into `SKILL.md`.

Save it verbatim under `references/` and write the body yourself. The reason is
authority, not tidiness. Whatever sits in the body is read as this agent's
standing instructions, and that text was written for a different harness: it
assumes tools that do not exist here, addresses an agent shaped differently, and
wherever it disagrees with Ava's own conventions the agent will follow it over
the system it is actually running in.

The demotion is mechanical, not just filing. Expansion injects `SKILL.md` and
nothing else — the files beside it stay on disk until something reads them. So
the same text moved one directory down stops being standing instruction and
becomes what it really is: material to consult. The body decides what to take
from it and says when to open it.

## Carry the mission-into-memory line

The card is durable by mechanism: establishing a context window re-lays it in
full, so it comes back after every compact. Per-agent memory is re-laid the same
way. The spawn prompt is not — once the window it arrived in is compacted away,
the mission survives only as whatever the summary happened to keep.

So every role card carries the same discipline: **on the first turn, write the
mission into your own memory** — what was asked, who for, what "done" looks like
— and keep it current as it changes. Without that line you get an agent that
remembers who it is forever and forgets what it was doing.

## Wire the preset

One preset per role, whose config names the card:

```bash
ava presets create --name <role> --label "<Role>" \
    --description "..." \
    --config '{"skills_to_expand_at_start":["be-a-<role>"]}'
```

Add `llm_model` / `reasoning_effort` if the role depends on them. Nothing else.

A name that does not resolve is warned and skipped, not an error — the agent
comes up without its role and looks fine. Register the card before the preset
points at it, and check the name against `ava.help(ava.skills)`.

## Skeleton

```
$AVA_HOME/skills/be-a-product-manager/
├── SKILL.md
└── references/
    └── <where-it-came-from>.md      # the external prompt, verbatim
```

```markdown
---
name: be-a-product-manager
description: <what this role is, and when an agent should be operating as it>
---

# <Role>

<Who you are. What you own. What you are accountable for.>

## How you work

<The standing method — the part that is true on every mission.>

## First turn

Write your mission into your memory: what you were asked to do, who for, and
what done looks like. Keep it current — it is the part of your context that
compaction can lose.

## Material

`references/<...>.md` — <what it is, and when to read it>
```

## Non-goals

- **The repo ships no role cards, and will not.** A card encodes one
  deployment's org, product and taste; a built-in `be-a-product-manager` would
  be a guess about someone else's company, shipped as authority. Cards are
  authored on demand — by an agent following this page — into the instance that
  needs them.
- **A role card is not a capability boundary.** Expanding a card mounts its full
  text; it does not touch the index. The index is universal by default, so
  adding `skills_to_inject_into_system_prompt` to a role preset can only
  *subtract* from what the agent sees listed — and even then only from the
  listing. Roles are not "given" skills, and a skill left out of a list still
  loads by name.
- **Not for one-off work.** A role that will be worn once is a spawn prompt, not
  a card and not a preset.
