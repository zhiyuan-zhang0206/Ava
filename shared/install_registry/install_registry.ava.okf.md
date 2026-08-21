---
type: doc
title: Install Registry (`installed.json`)
description: '`shared/install_registry.py` — the machine-local, NOT DB-backed registry of externally installed skills / plugins / MCP packages. It is also the gate: `$AVA_HOME/skills/` is the single skill load dir, and only registered + enabled entries are scanned.'
tags:
- shared
- library
- packaging
---

# Install Registry (`installed.json`)

## What it is

`shared/install_registry.py:Registry` is a flat list persisted at
`$AVA_HOME/installed.json` — **machine-local, not DB-backed**. External packages
are installed per machine, never pushed from the cluster, and they must land on
an **agent-runner**: skills and MCP servers are consumed inside the agent
process (the skill scanner and `ava/_mcp_config.py` run there), so a package
dropped into a gateway-only host's `$AVA_HOME/plugins/` is never scanned.

## Entry shape

`{name, type, source, path, ref, enabled, origin, origin_path, content_hash,
installed_at, updated_at, trust, scanned_at, accepted_findings}`

- `type` — `skill` | `plugin` | `mcp`
- `source` / `path` / `ref` — the git URL, the subdir within it, the pinned ref
- `origin` / `origin_path` / `content_hash` — converge provenance written by
  `cli/commands/_converge_skills.py`; `shared/install_registry.py:tree_hash()` recomputes
  the hash to surface a `modified_locally` drift flag
- `trust` / `scanned_at` / `accepted_findings` — the content trust tier, when
  `shared/skill_scan.py` last read the package, and the rule ids a human waived
  with `--accept-risk` (see below)

## Trust tiers

`trust` records how far a package's *content* may be trusted, written where the
content enters; `trust_by_name()` classifies a whole tree in one read.

| tier | means | written by |
|---|---|---|
| `builtin` | came out of this Ava checkout (`ava_builtins/`) | converge |
| `reviewed` | a human here read the content and vouches for it | `ava skill trust <name>` |
| `unreviewed` | ingested from outside — treat as attacker-controlled | every install path |

A **clean scan does not promote**: an install matching no rule still lands
`unreviewed`, as does an `--accept-risk` install. Converge owns the `builtin`
stamp only, so a `reviewed` promotion survives every pass. A row predating the
field reads `unreviewed` until the next converge — the safe direction.

Its consumer is any runtime layer pulling skill text into a context **without a
human choosing it** (skill recall): `unreviewed` content may be named, not
auto-injected. An untracked skill — a plugin's runtime provider root — counts as
`unreviewed`.

## The registry is the gate, not a record

`$AVA_HOME/skills/` is the **single skill load dir**, and
`ava/skills.py:_scan_tree` surfaces a top-level directory only when its name is
in `enabled_skill_names()` (tracked **and** `enabled`). A directory that isn't
registered — or whose entry is `enabled=false` — is skipped. That is what stops
a stray hand-copied tree from silently loading; `ava skill register <name>`
adopts one deliberately.

Repo skills (`<repo>/ava_builtins/skills/`) and plugin skills
(`<repo>/ava_builtins/plugins/<p>/skills/`, `$AVA_HOME/plugins/<p>/skills/`) are
synced into that dir by the skills converge step; an installed plugin's skills
gate on the plugin's own registry entry.

Installed packages activate on the **next skill scan — no restart**: the scanner
reads the registry on every call, and `ava plugins install|upgrade` runs the
converge pass inline.

## One way to write it
The single write path — `mutate()` under `registry_lock`, its wrappers, and the three bulk-edit cycles: [[shared/install_registry/write-path.ava.okf.md]].

## Installable shapes

- **skill** — a git repo (or `--path` subdir) whose root holds a `SKILL.md`,
  cloned into `$AVA_HOME/skills/`.
- **Claude Code plugin** — a directory holding `.claude-plugin/plugin.json`,
  materialized under `$AVA_HOME/plugins/<name>/` by
  `cli/commands/_claude_code_plugin.py`. It may bundle any of: `skills/`
  (copied verbatim), `agents/` (turned into one orchestrator skill),
  `commands/` (copied verbatim, surfaced as composer `/`-commands by
  `ava/_commands.py:discover_commands`), or a root `.mcp.json` (merged by
  `ava/_mcp_config.py:load_mcp_config`). A hooks-only plugin bundling none of
  these is refused.
- **MCP package** — a self-contained package (its own `.mcp.json` +
  `pyproject.toml`) under `$AVA_HOME/mcps/<name>/` with an isolated `.venv`.

## The install-time scan

Nothing is sandboxed, so the trust decision is made before the copy. Every
package entering from outside the checkout — `ava skill install`, both branches
of `ava plugins install`, and `ava skill register` (a hand-copied dir reaches
the same load dir, so it meets the same gate) — is read file by file by
`shared/skill_scan.py`, including inside base64 / hex / percent-encoded blobs,
decoded recursively before matching. A **critical** finding refuses the install
with a file/line report and writes nothing; `--accept-risk` overrides and
records the waived rule ids. **Notice** findings never block — they are what a
reviewer reads before `ava skill trust`. `ava skill scan <name-or-path>` re-runs
it on demand (exit 2 on criticals).

A **mitigation layer, not a boundary**: known shapes of known attacks, in text
it can read. A clean report means "no rule matched", never "safe". Rationale +
rejected alternatives:
[the decision record](../../decisions/2026-07-29-skill-trust-tiers-and-install-scan.md);
open gaps: [what's left](../../future/infra/skill-supply-chain-trust.md).

Beyond the load dir, a plugin can contribute skill roots at scan time via
`ava/skills.py:register_skill_source` — the `ava_code` plugin uses it to surface
the working repo's `.agents/skills` / `.claude/skills` as `ava.cwd` moves between
projects.

## Key Dependencies

- [[cli/commands/packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` operator surface
- [[plugins_config.ava.okf.md]] — the sibling per-machine plugin enable config
- [[okf/skills/skills.ava.okf.md|Skills]] — what a skill is and how the scanner loads one
- [[okf/mcps/mcps.ava.okf.md|MCP integration]] — MCP server merge layers and launch form
