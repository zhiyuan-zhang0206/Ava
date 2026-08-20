# Stop fleet-distributing kernel-contributor skills — sequenced after the extension registry

## Context

[`2026-08-19-four-layer-modification-model.md`](2026-08-19-four-layer-modification-model.md)
moved `ava-self-development` into the kernel-contributor skill family
(`.agents/skills/`), marking it L4-only in the skill index, but left open
whether that family should keep reaching every runtime agent. Converge syncs
both `ava_builtins/skills/` and the non-symlink entries of `.agents/skills/`
into every runtime agent's skill load directory (the R5 design, task #1013;
`cli/commands/_converge_skills.py:iter_sources`), so the L4 marking changes how
an agent routes past the skill, not whether it lands on the machine at all.
That entry recorded three options without deciding among them: (1) status quo,
(2) stop converging `.agents/skills/` and rely on the project-local mount
(`project_skill_roots`, `ava_builtins/plugins/ava_code/_walk.py:70`) so only an
agent working inside the repo checkout sees these skills, (3) a per-skill
opt-out marker.

## Decision

**Option 2 is ruled**, sequenced **after the cluster registry from issue #39 /
`future/infra/extension-ownership.md` lands** — specifically its S2 slice,
which moves skill distribution off per-machine converge onto cluster registry
rows and is the natural place for this change to ride, rather than being built
twice. Until S2 lands, option 1 (status quo) is the interim state:
kernel-contributor skills stay fleet-distributed, index entries carry the L4
marking. The work itself is tracked in issue #146, blocked on S2.

## Alternatives rejected

- **Option 1 as the permanent answer.** Every runtime agent's skill index
  keeps carrying kernel-contributor noise it will never act on. Accepted only
  as the interim state until S2 lands.
- **Option 3 (a per-skill opt-out marker).** A permanent knob to solve what
  becomes, once the registry redesign lands, an already-fixed problem —
  against the small-core principle (`conventions/philosophy.md`).
- **Doing option 2 now, standalone.** It would change fleet skill distribution
  and partially reverts the R5 design (task #1013) on its own, ahead of the
  broader redesign already scoped for that exact surface (S2 replaces
  converge-driven distribution with registry rows). Riding S2 means the
  distribution mechanism changes once, not twice.

## Consequences

- Resolves the open point in
  [`2026-08-19-four-layer-modification-model.md`](2026-08-19-four-layer-modification-model.md)
  (forward-linked from there); that entry's other content — the four layers,
  the skill restructuring, the known gaps — stands unchanged.
- `okf/skills/load-directory-sync.ava.okf.md`'s description of `.agents/skills/`
  fleet convergence remains accurate until S2 lands and this ruling is acted
  on; it is revised in the same change that implements the stop.
- Issue #146 stays blocked until S2 (`future/infra/extension-ownership.md`)
  lands; it is not scheduled independently.

Related: `decisions/2026-08-19-four-layer-modification-model.md` (the decision
this resolves), issue #39 and `future/infra/extension-ownership.md` (the
registry this rides), issue #146 (the tracked follow-up work).
