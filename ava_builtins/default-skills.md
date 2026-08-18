# Default bundled skills — which capability packs ship with the Ava repo

> Design draft (2026-06-01). Direction only — decides *which* external skill
> packs we adopt as repo defaults and *how* we carry them, not their wording.
> Awaiting review.

## Why now

PR #674 wired the two consume paths for Claude Code capability packs:

- **skills-only plugins** install (`_claude_code_plugin.materialize()` copies a
  plugin's `skills/` verbatim into the overlay) — so a pure-skills pack like
  **superpowers** is now installable.
- **standalone MCP servers** via `ava mcp add` into machine `mcp.json`.

That answers "*can* we install it." This doc answers the next question: of the
packs out there, **which should every Ava agent have by default**, and do we
*vendor* them into the repo or merely *recommend* an `ava plugins install`.

## What "default" can mean (the carry decision)

| Option | Where it lives | Trade-off |
|---|---|---|
| **Vendor into repo** | `<repo>/ava_builtins/skills/<name>/` or a built-in plugin under `<repo>/ava_builtins/plugins/` | Always present for every agent, version-pinned, reviewable in-tree, adaptable to Ava idioms. Cost: repo bloat + we own keeping it synced with upstream. |
| **Recommend install** | documented `ava plugins install <url>` in the runbook; lands in `~/.ava/plugins/` per machine | Stays upstream (no fork drift), no repo bloat. Cost: not guaranteed present, per-machine, unadapted (see cross-reference problem below). |

These are the poles; a middle path is "vendor a curated subset, recommend the
rest." The decision is per-pack, not global.

### Direction (settled 2026-06-01): vendor-and-adapt

> **Exercised 2026-07-30 by the first pack** — `ava-ui/design` +
> `ava-ui/dataviz`, vendored from the Claude Code bundled skills
> `artifact-design` and `dataviz`, and **removed again 2026-08-14** when the
> open-source publish put its license basis under scrutiny. See "First vendored
> pack" below for what the adaptation cost, the conventions it established, and
> the one that cost the pack.

A fresh `ava plugins install` of an upstream pack is **limited** — its skills
carry Claude-Code-specific content (the `Skill` tool, native worktrees, the
`superpowers:` cross-reference namespace) that doesn't hold in Ava (see the
cross-reference section). So the default posture is **vendor into the repo and
adapt**: strip the CC-specific parts, rewire to Ava idioms, and maintain the
result in-tree. We own the sync cost, but every agent gets a pack that actually
works here. Recommend-install stays available for niche packs not worth
adapting.

## Candidate: superpowers

The pack the user named. It's a coherent set of **engineering-workflow
disciplines** (not domain tools) — the kind of thing that benefits from being
present by default because it shapes *how* the agent works, not just *what* it
can do. Its skills group roughly as:

- **Process discipline** — brainstorming, writing-plans, executing-plans,
  test-driven-development, systematic-debugging, verification-before-completion
- **Collaboration / review** — requesting-code-review, receiving-code-review,
  subagent-driven-development, dispatching-parallel-agents
- **Meta** — writing-skills, using-superpowers, using-git-worktrees,
  finishing-a-development-branch

### Overlap with what we already ship
The repo already has skills that cover some of this ground — adopting
superpowers wholesale would create **two skills for one job**, which the scanner
surfaces as competing entries. Some overlap is **unavoidable** — the goal is to
*minimize* it, not eliminate it; chasing zero overlap isn't worth the cost.
Reconcile the clear pairs before vendoring, accept the fuzzy ones:

- `skill-creator` ↔ superpowers `writing-skills`
- `ava-self-development` (+ the worktree discipline in our memory/CLAUDE.md) ↔
  superpowers `using-git-worktrees` / `finishing-a-development-branch`
- `ava_fleet` (the fleet orchestration skill; teamwork folded in) ↔ superpowers
  `subagent-driven-development` / `dispatching-parallel-agents`

Per pair, pick the cheaper of **adopt-and-retire-ours**, **keep-ours-and-skip-theirs**,
or **merge the best of both** — whichever most reduces duplication without
forcing a rewrite.

## The cross-reference problem (why raw install is lossy)

superpowers skills are written for Claude Code: they reference each other by the
`superpowers:<name>` namespace and assume Claude-Code-native tools (the `Skill`
tool, native git worktrees). In Ava they materialize as plain `ava.skills.*`
with no namespace, and the tools differ (`ava.agents.spawn`, `EnterWorktree`).
So an unadapted install leaves **dangling cross-links and wrong tool
references** — which is exactly why the settled direction is vendor-and-adapt
(delete the CC-specific parts, rewire cross-links to `ava.skills.*`) rather than
raw recommend-install. Adaptation is real work, not hand-waving: quantifying it
(links/tool-refs per skill → effort estimate) is the first implementation step.

## First vendored pack (2026-07-30, removed 2026-08-14) — frontend design

The vendor-and-adapt path is no longer theoretical. Two Anthropic-authored
Claude Code bundled skills lived in-tree as sub-skills of `ava-ui` for two weeks:
`ava-ui/design` (from `artifact-design`) and `ava-ui/dataviz` (from `dataviz`).

**They were removed on 2026-08-14.** Both were copied out of the closed-source
Claude Code CLI binary, and the recorded Apache-2.0 basis — a *similarly named*
public sibling in `anthropics/skills` — did not survive checking: that repo
carries neither skill. The mechanics below are what the exercise settled and
still hold; the license lesson is the one that cost the pack. Detail:
[`ava_builtins/skills/VENDORED.md`](skills/VENDORED.md).

What it settled, beyond the direction:

- **License is a gate, not a footnote — and it gates the *source*, not the
  content.** Everything else on this list is about whether a pack *works* here.
  This one decides whether it may ship at all, and it is the cheapest check to
  run first: name the upstream repo, open its LICENSE, confirm it covers the
  files you copied. A pack extracted from a closed-source binary has no such
  repo to point at, which is the answer. Adjacency is not a license — the
  strength of the adaptation, or of the content, does not enter into it.

- **Adaptation cost is small when the pack is prose.** The predicted expense was
  cross-links and tool references. In practice the design content was almost
  entirely portable: the harness-specific surface was the Artifact publishing
  flow, one CSP claim, one platform theme toggle, and five path references. The
  *value* — the calibration, typography, color, layout, and quality-floor
  material — needed no change. Packs that are mostly judgment travel well; packs
  that are mostly tool-driving (the superpowers workflow skills) will not.
- **Placement is a recall decision, not a taxonomy decision.** Written when the
  capability index was a 5-name whitelist, so a new top-level skill was invisible
  until `AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT` changed on every cluster, and
  nesting under an already-indexed entry skill was the only reliable trigger.
  That default is now `*` — every loaded skill is indexed, including a new
  top-level one, so placement no longer gates visibility. The conclusion holds
  anyway on the weaker ground it always also stood on: an index line is a
  one-liner the agent has to match against its task, and a pack reached by a
  chain pointer from the skill that owns its job is recalled at the moment it is
  relevant. So: **vendor a pack under the skill that owns its job**, and promote
  to top-level when it has no natural parent.
- **Soft pointers, not mandatory ones.** The chain line in `ava-ui` was advisory
  ("for a polished or user-facing page, consider loading …"). A mandatory load
  would tax every trivial render; the vendored skill already carried its own
  how-much-design-does-this-warrant calibration, so the judgment belonged to the
  agent reading it. (A side benefit only visible at removal: an advisory pointer
  is one line to delete, so a pack that has to leave does not strand callers.)
- **Vendored payload stays byte-identical.** Upstream files were carved out of
  ruff/pyright in `pyproject.toml` so a future sync would diff cleanly.
  Reformatting vendored code is what makes a fork undiffable, and it is the
  default outcome unless explicitly prevented. It also makes provenance
  legible after the fact — byte-identity to the closed-source original is
  precisely what made the license question answerable rather than arguable.
- **Vendoring bypasses the supply-chain scanner.** Anything under
  `ava_builtins/skills/` is stamped `trust="builtin"` by converge, so a vendored
  pack must be read in full before commit — the scanner will never see it.

Open follow-up: `VENDORED.md` is a hand-maintained manifest. A tracker that
diffs recorded upstream versions against current upstream is the natural next
step, and is why the manifest records versions rather than just sources. It was
never built — and would not have caught this anyway: drift tracking assumes the
vendoring was legitimate to begin with, which is the check that was missing.

## Other packs to weigh (not yet evaluated)

Left open for review to add/cut. The bar for a *default*: it shapes how every
agent works (broad), not a niche capability (that's recommend-install or
on-demand). MCP servers are a separate axis — covered by [`../future/infra/mcp-scope-and-bundling.md`](../future/infra/mcp-scope-and-bundling.md).

## Open questions for review

Settled (2026-06-01): vendor-and-adapt over raw install; minimize overlap rather
than eliminate it. Still open:

1. For each overlap pair above — adopt theirs, keep ours, or merge?
2. ~~If we vendor, where~~ — answered for a pack with a natural parent skill:
   nested under that skill in `<repo>/ava_builtins/skills/`, for the recall
   reason in "First vendored pack". Still open for a pack with **no** natural
   parent (a standalone `superpowers` tree): loose top-level skills, or a
   built-in plugin under `<repo>/ava_builtins/plugins/`. (No longer a whitelist
   question — the index is universal; the question is what shape the tree takes.)
3. What else belongs on the default list beyond superpowers?
4. Adaptation effort estimate (links/tool-refs per superpowers skill) — the
   gate on committing to the full vendor. The frontend pack came in cheap, but
   it is prose; superpowers is tool-driving, so it does not transfer as an
   estimate.

## Related

- PR #674 — the install mechanism this builds on
- [`../future/infra/decentralized-install-and-config.md`](../future/infra/decentralized-install-and-config.md) — install registry / overlay model
- [`../future/infra/mcp-scope-and-bundling.md`](../future/infra/mcp-scope-and-bundling.md) — the MCP-bundling axis
