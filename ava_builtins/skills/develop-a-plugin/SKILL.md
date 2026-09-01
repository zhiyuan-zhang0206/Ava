---
name: develop-a-plugin
description: Develops Ava plugins locally, verifies them across restart, and promotes stable plugins to independent git repositories. Use when creating or changing an Ava plugin, even if the user initially calls it a feature or integration.
---

# Develop a plugin (L3)

Developing a plugin is **L3** of the four-layer modification model (see the
`ava-modification-layers` skill): it happens entirely inside your deployment,
gated by the deployment owner. It does **not** ride the kernel pipeline — no
worktree/PR/CI against the kernel repo, no `ava cluster update`. Design record:
`decisions/2026-08-19-four-layer-modification-model.md`.

**Plugins live in their own repos** (user ruling, issue #42). The builtin
plugins under `<repo>/ava_builtins/plugins/` are a kernel-shipped base set —
like in-tree drivers — and change via L4; everything new starts external.

## The ladder

```
write locally  ->  test at your own restart  ->  when stable: its own git repo
               ->  install/enable on the machines that need it  ->  maintain
                   per-plugin (upgrade/rollback), never via cluster update
```

### 1. Write locally (no git needed)

An external plugin is a directory `~/.ava/plugins/<name>/` with a `plugin.py`
(entry point; hooks / SDK namespaces / config schema / services register from
here) and optionally `setup.py` (idempotent `scaffold()`, run by explicit
`ava memory init`),
`skills/` (skill dirs converge syncs into the load dir), and `default_config.py`.
Plugin discovery is a filesystem scan — a hand-placed directory is found; no
registration step is needed to develop. Look at a builtin under
`<repo>/ava_builtins/plugins/` (e.g. `ava_code`) for the shape, and at
`conventions/plugin-spec-v2.md` for the contribution-surface vocabulary.

Enable it on this machine: `ava plugins enable <name>` (writes the per-machine
`plugins_config.json`).

### 2. Test at the restart boundary

**The reload boundary is the agent process's `self.restart`** (user ruling
2026-08-13, recorded in `conventions/plugin-spec-v2.md`). There is no
in-process hot reload: after editing plugin code, restart yourself
(`ava.self.restart(...)`) and the fresh process loads the new code. Canary it
on yourself first; today enablement is per-machine, so every agent on the
machine loads an enabled plugin — per-agent activation (enable for ONE agent,
widen later) is designed in issue #39 and not built yet. A plugin that fails
at load time therefore hits every agent on the machine: keep the module
import + `plugin.py` entry cheap and fail-fast.

### 3. Promote to its own repo when stable

Create a git repo for it — public or local; a local bare/plain repo is enough
(`git init`, commit). Add an `ava-plugin.json` manifest at the package root
(`conventions/plugin-spec-v2.md`): identity, version, `engines.ava` range,
dependencies. The manifest is validated at install time where the install
flow supports the package kind.

**Current gap, stated honestly:** `ava plugins install <url>` today recognizes
bare-skill packages and Claude Code plugin packages; a native `plugin.py`
package is not yet installable through it (the manifest-driven native install
is plugin-spec-v2 S3+ work). Until that lands, the durable install of a native
plugin is: clone its repo to `~/.ava/plugins/<name>` on each machine that
should run it, then `ava plugins enable <name>`. Upgrade = `git -C
~/.ava/plugins/<name> pull` + restart the agents; rollback = `git checkout
<tag>` + restart. Third-party code installed this way bypasses the
supply-chain scan that `ava plugins install` runs — read it in full first.

### 4. Maintain per-plugin

Version, upgrade, and roll back the plugin against **its own repo**, at the
restart boundary. `ava cluster update` never touches external plugins — it is
the L4 kernel rollout. Disable is `ava plugins disable <name>`; removal of a
registry-tracked install is `ava plugins uninstall <name>`.

## What this is not

- Not for changing a **builtin** plugin — that is a kernel change (L4, the
  `ava-self-development` workflow in the kernel repo).
- Not a way around the restart boundary — do not design for in-process reload;
  if something looks like it needs one, the answer is a restart.
- Not an MCP server package — a standalone MCP installs with `ava mcp install`
  and has its own lifecycle.
