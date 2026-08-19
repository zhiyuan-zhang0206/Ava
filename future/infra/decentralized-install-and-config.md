# Decentralized install + local config for plugins / skills / MCP

> **Status: landed. One thing is left — hooks-only plugin bundles.**
>
> Piece A (install registry + `ava plugins install/uninstall/installed/upgrade/
> enable/disable` + overlay reservation) and Piece B (Claude Code plugin
> materialization) shipped, as did the standalone `type="mcp"` registry package
> (`ava mcp install`, third MCP source — see
> [`mcp-scope-and-bundling.md`](mcp-scope-and-bundling.md)) and the cross-machine
> inventory UI (`/control/inventory` + `GET/PUT /api/inventory`).
>
> Two directions reversed versus the original draft. Plugin-enable state went
> **per-machine-local** (`plugins_config_overrides` dropped). And the override layer
> as a whole is **retired** — config is single-source in each unit's `.env`, so the
> "runtime config stays cluster-backed in a DB row" position below is history; see
> [`2026-07-19-config-ownership-decomposition.md`](../../decisions/2026-07-19-config-ownership-decomposition.md).
>
> Original 2026-05-30 framing kept for rationale: it supersedes an earlier
> "capability-gating central distribution" framing — the gating problem dissolves
> once install is a local operation.
>
> **Proposed partial reversal (issue #39)**: the "install is a local operation"
> half is proposed to change — install would write cluster registry rows + blobs,
> per-machine enable files would demote to materialized caches, and enable state
> would return to the cluster. The agent-driven-imperative-install and
> single-source-`.env` rulings stand. See
> [`extension-ownership.md`](extension-ownership.md).

## The reframe

Earlier framing: the cluster pushes capability-gated config to machines. Wrong.
The real model: **installing a plugin / skill / MCP is a local operation.** The
gap that bit us during the self-update rollout (`ava.self.update()`, removed
2026-08) is not config distribution — it's
that these resources have no mature install/upgrade system yet.

Per-machine "availability" then isn't a gating problem: a `browser-use` MCP
server lives on GUI machines because you only **install** it there.

## Units & bundling (mirrors Claude Code)

- **skill** (SKILL.md + files), **MCP** (server config / process), and Ava's
  plugin hooks are independently usable primitives.
- **plugin = a bundle** that MAY ship skills + MCP + hooks together (CC's plugin
  bundles `skills/` + `.mcp.json` + hooks + agents under one manifest). A plugin
  is a packaging/distribution mechanism, not a fundamental unit.
- **Repo-local agent skill folders:** beyond static plugin-bundled skills, a plugin
  can contribute skill roots resolved at scan time
  (`ava/skills.py:register_skill_source`). The `ava_code` plugin uses it to load the
  working repo's own `.claude/skills` (Claude Code compat), `.agents/skills` (the
  open Agent Skills standard directory) and `.ava/skills` (Ava's, scanned last so it
  wins) as the agent points `ava.cwd` at a project — so a repo ships skills specific
  to working *on that repo*, git-tracked and riding its own `git pull`, separate from
  Ava's built-in set. Other agent tools' folders (`.codex/...` etc.) can join the
  same provider later.

## Install = local, agent-driven

- A package is installed from a **source** (a git repo / URL / marketplace) into
  the machine's `$AVA_HOME` overlay. The overlay is **only** for externally-installed
  packages — repo content never belongs in it, which is what dissolved the
  stale-shadow problem.
  *Superseded for skills (2026-07-14): `~/.ava/skills/` is now the single load dir;
  repo/plugin skills are converged into it (registry-tracked, hash-guarded) rather
  than loading from the repo. See
  [`2026-07-14-skills-single-load-dir`](../../decisions/2026-07-14-skills-single-load-dir.md).*
- The **installer is an Ava agent, not a declarative engine.** CC's
  `/plugin install` is a fixed pipeline that can't debug. Ava's strength is an
  agent with terminal + code that can clone, run setup, hit an error, read it,
  fix, retry. Install becomes a task an agent performs (guided by an install
  skill); upgrade = re-fetch from the recorded source. Versioning is
  source-level (tag / commit), like CC — record `{source, ref}` per installed
  package, no heavy lockfile.

## Config & enablement = local

> Reconciliation with the four-quadrant frame
> ([`2026-05-22-state-surface-canonicalization.md`](../../decisions/2026-05-22-state-surface-canonicalization.md)):
> that draft put plugin config in Q4 (cluster-wide → DB); the move here reclassifies
> it as **per-machine**, so it's local overlay config, not a DB row. The frame and its
> "`~/.ava/` must be wipe-and-replay safe" rule still hold — the overlay just also
> legitimately holds registry-tracked external installs (a *governed* per-host
> location, not an ungoverned third place).

- Plugin enable/disable and settings are **per-machine local config.** There is
  no cluster-shared config row. `installed` ≈ available; enable/disable is a
  local toggle in the machine's install registry / config.
- **`plugins_config_overrides` is gone.** Rationale: a single cluster row mismatches
  per-machine install (enabled-but-not-installed dangling), and an offline machine
  should not surface in settings at all. Config lives on the machine; each machine
  reads its own at boot.
- **Runtime config was kept cluster-backed for a while, then dropped too.** The
  2026-06-03 decision kept `runtime_config_overrides` because runtime config has a
  deliberate cluster-distribution mechanism (the bootstrap handshake) that plugins
  never had, and cluster-identity fields cannot meaningfully be per-machine. What
  generalized out of that argument and survives is the **ownership taxonomy**: every
  Settings field declares a `scope` (`cluster-pinned` / `cluster-default` / `host` /
  `agent`), and `BOOTSTRAP_FIELDS` is derived from it (`shared/config/`). The
  override *store* was then retired on 2026-06-08 — cluster fields live in the
  gateway's `.env`, host fields in the machine's own, precedence collapsed to
  `env(.env) > default`, and `ava config set` is the edit path.
- **The "one switch for many machines" convenience is a frontend concern**, not a
  data-model one — and it shipped as `/control/inventory`: a matrix of plugins / MCP
  servers × machines over `GET /api/inventory`, where a cell click `PUT`s a
  single-item delta to that one host and enable-all fans out one `PUT` per installed
  host. The backend still only ever handles separate per-machine writes (the host
  validates against its own reality and disposes), and a cell the host can't enable
  is pre-greyed with its reason. One deviation from this draft: unreachable machines
  are shown as a muted `?` column rather than omitted — being told a host's state is
  *unknown* turned out to be more useful than silently dropping it.
- Host runtime fields (port, Chrome path, concurrency) are per-machine
  **individual** — the value on one machine has no meaningful relation to another's —
  so they use a machine **selector** (`GET/PUT /api/config?machine=<name>`), not the
  same-key-collapse matrix. `remote_writable` is a default-deny allowlist enforced by
  `scripts/lint_config_scope.py`; identity/connection fields are never remotely
  editable, because editing them would brick the host.

## Fleet install = spawn an agent per machine

"Install X on all GUI hosts" = spawn an installer agent on each target machine;
each runs the local install, **decides locally whether it applies** (e.g. checks
for a GUI before installing browser-use), and debugs its own failures. No central
distribution daemon — it reuses the core spawn primitive. The per-machine
capability decision lives in the installing agent, not a central table.

## Trust / security

- **There is no sandbox around installed code** — it runs directly in the agent
  process, on the host, exactly like any other `execute_code` call (see
  [`SECURITY.md`](../../SECURITY.md)). Containment, where it exists, is
  deployment-level (a dedicated user/machine/VM per cluster), not something the
  install path adds on top.
- Default trust = the human vets the source and hands the link to an agent to
  install (≈ CC's "trust the marketplace you added"). Source-level trust is the
  actual boundary here — install does not layer a sandbox on top of it.
- An automated security-review agent (screen a source before install) is a
  possible **later** enhancement, not v1 — it reduces risk but can't prove
  arbitrary code safe.

## What this dissolves

- Capability-gating tables → gone (the capability check moves into the
  installing agent).
- Central config push → gone (config local; fleet ops via spawned agents).
- Stale `~/.ava/skills/` shadow → a non-issue once the overlay is governed by the
  install registry.

## Still open: hooks-only plugin bundles

`cli/commands/_claude_code_plugin.py` materializes whatever a Claude Code plugin
bundles — `skills/` copied verbatim, `agents/` into a generated orchestrator skill,
`commands/` into composer `/`-commands, a root `.mcp.json` into the overlay MCP
source — and **refuses a plugin that bundles none of those**, i.e. a hooks-only
plugin. The blocker is not the adapter but the lifecycle: a Python-hook plugin needs
an agent restart to load, so "install succeeded" would not mean "hook active". That
is the piece to design.

Also unresolved: registry enable/disable gating for `~/.ava/plugins/` dirs (today
install == enabled).
