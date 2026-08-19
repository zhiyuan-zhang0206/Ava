# The four-layer modification model: install / skill edit / plugin development / kernel change

## Context

Agents and users modify an Ava deployment at four distinct layers, but the
machinery and the docs welded them together (issue #42). The conflation was
concrete: `ava-self-development` — kernel-contributor discipline whose content
is L4-only (worktree → PR → CI → merge → `ava cluster update`) — shipped as a
runtime builtin skill to every agent; and because all builtin plugins live in
the kernel repo, conceptually-L3 work (changing a plugin) was forced through
L4 machinery (kernel PR + CI + human merge + cluster rollout). After
open-sourcing, the expectation is that ~90–95% of users live at L1–L3 and
never touch L4 — the layering must serve them, not just the kernel author.

The dsh (DeepSeek Harness) comparison supplied the framing: its promotion
ladder (ephemeral probe → durable preset → package bundle) shows the value of
naming each tier's legitimate use and its promotion path. Ava's tiers are
L1–L4 with durable landing zones.

## Decision

The four layers, each with its own medium, apply mechanism, and gate-holder:

| Layer | Medium | Applies via | Gate-holder |
|---|---|---|---|
| **L1 — install** an existing extension (plugin / skill / MCP server) | `ava plugins install` / `ava skill install` / `ava mcp install` | next skill scan / next use | deployment owner (install-time supply-chain scan) |
| **L2 — edit a skill** | SKILL.md files | invocation-time — skill bodies are read fresh (mtime-cached `SkillIndex`); the system-prompt index and other agents catch up at compact/spawn | the agent / deployment owner |
| **L3 — develop a plugin** | the plugin's own repo / `~/.ava/plugins/<name>/` | the agent process's `self.restart` boundary (ruling 2026-08-13, `conventions/plugin-spec-v2.md`) | deployment owner |
| **L4 — change the kernel** | the upstream kernel repo: issue / PR / fork | PR → CI → human merge → `ava cluster update` | upstream maintainer + CI |

Core rulings:

1. **L3 plugins live in their own repos, not the kernel repo.** The
   development ladder: write locally (no git needed — a hand-placed
   `~/.ava/plugins/<name>/` is discovered by the filesystem scan) → test
   across one's own `self.restart` → when stable, create a git repo (public
   or local) → install via the normal install flow (the `ava-plugin.json`
   manifest carries identity/version/deps) → maintain per-plugin
   (upgrade/rollback) at the restart boundary. Plugin lifecycle is thereby
   fully decoupled from `ava cluster update`.
2. **Builtin plugins remain a kernel-shipped base set** (like in-tree
   drivers), changed via L4. New and user-developed plugins are external.
   Recorded explicitly so "should builtins move out?" stops being
   re-litigated.
3. **L4 is the sole province of `ava cluster update`** (plus routine version
   tracking). No other layer involves a cluster rollout.

Skill restructuring that lands with this entry:

- `ava-self-development` (the L4 manual) moved from `ava_builtins/skills/` to
  `.agents/skills/` — the kernel-contributor family, beside `ship-a-change` /
  `write-a-pr-description`.
- Runtime gains a thin replacement, `ava-modification-layers`: the four-layer
  model, the "never treat the prod checkout as a workspace" warning (runtime
  agents have shell access to `~/.ava/source`; this incident class is real,
  so the warning must stay reachable at runtime), and "kernel problems
  escalate to L4 via issue/PR".
- New runtime skill `develop-a-plugin`: the L3 ladder above.
- `ava-self-evolution`'s "what changed this week" step widened from
  kernel-repo `git log` to also sweep the install registry
  (`$AVA_HOME/installed.json`) and hand-cloned external plugin repos —
  otherwise the optimizer is blind exactly where user modification
  concentrates (L1–L3).

### Open point: family placement is taxonomy, not distribution

Converge syncs **both** repo-native skill sources — `ava_builtins/skills/` and
the non-symlink entries of `.agents/skills/` — into `$AVA_HOME/skills/` (the
R5 design, task #1013; `cli/commands/_converge_skills.py:iter_sources`). So
moving `ava-self-development` into the kernel-contributor family does NOT stop
it (or `ship-a-change` etc.) from reaching every runtime agent's skill index;
issue #42's "stop shipping it as a runtime builtin" is only partially achieved
— the index entry now marks it L4-only, but it still lands in the load dir.
Options, deliberately not decided here:

1. **Status quo** — kernel-contributor skills stay fleet-distributed; their
   index lines say "L4 / kernel-contributor" so agents route past them. Cheap,
   slightly noisy index.
2. **Stop converging `.agents/skills/`** — rely on the project-local mount
   (`ava_code`'s `project_skill_roots`: an agent working inside the repo sees
   them anyway). Cleanest layering; changes fleet skill distribution and
   partially reverts R5, so it needs its own ruling.
3. **A per-skill opt-out marker** in `.agents/skills/` entries that converge
   skips. Precise but adds a knob.

### Known gaps (recorded, not built here)

- **L2 cross-agent broadcast**: when a skill changes, other live agents get no
  notification; their system-prompt indexes rebuild only at compact/spawn
  (deliberate — prompt-cache economics). The mechanism, when built, is a plain
  message ("skill catalog changed, re-read on demand") riding issue #39-S2's
  cluster skill-sync event — a message enters context without touching the
  cached prompt prefix; no second channel, and prompt rebuilds stay at
  compact/spawn. Blocked on #39-S2.
- **L3 native-plugin install flow**: `ava plugins install` recognizes
  bare-skill and Claude Code packages only; a native `plugin.py` package
  installs today by cloning into `~/.ava/plugins/` (bypassing the scan gate —
  `develop-a-plugin` says so). The manifest-driven native install is
  plugin-spec-v2 S3+ work.
- **Change-detection registry feed**: the self-evolution sweep reads the
  per-machine install registry; when #39's cluster registry lands, its
  version-change feed replaces that per-machine sweep as the authoritative
  source.

## Alternatives rejected

- **Keep everything kernel-resident** (status quo ante): welds L3 to L4 —
  every plugin tweak pays PR + CI + cluster update and requires upstream merge
  rights. Untenable for non-author users, and it makes the kernel repo accrete
  deployment-specific plugins, against the kernel-stays-general framing.
- **In-process plugin hot reload for L3** (skip the restart): already rejected
  (ruling 2026-08-13, recorded in `conventions/plugin-spec-v2.md` — the reload
  boundary is `self.restart`). Nothing here reopens it.
- **Hot-reload the skill directory into the system prompt for L2**: rejected —
  it would invalidate the session's prompt cache on every file edit;
  system-prompt updates are deliberately confined to compact/spawn. Skill
  *bodies* are already invocation-time fresh; the directory catches up at the
  next compact/spawn, and the (future) broadcast message covers the gap.
- **Fold self-development into the self-evolution skill**: buries L4 safety
  discipline inside a weekly-cron workflow where ad-hoc coding agents won't
  look. The two are different roles (optimizer loop vs. apply mechanics) that
  compose, not nest.

## Consequences

- A plugin author never needs kernel merge rights; the kernel repo stops being
  the landing zone for deployment-specific plugins.
- The self-evolution optimizer must look beyond the kernel repo's `git log`,
  or it goes blind to L1–L3 change (the skill text now says so).
- Two skills describe changing Ava (`ava-modification-layers` at runtime,
  `ava-self-development` for kernel contributors) — the split is by audience;
  the thin one must keep routing to the full one.
- Until the open point above is settled, kernel-contributor skills remain
  visible in every agent's index; their descriptions carry the L4 marking.

Related: issue #42 (this decision), issue #39 (cluster registry / skill sync /
per-agent activation), `conventions/plugin-spec-v2.md`,
`decisions/2026-05-09-self-rolling-release.md`,
`decisions/2026-05-13-plugin-and-hook-layers.md`.
