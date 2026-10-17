---
name: develop
description: "Develops and verifies native Meiva plugins. Use when creating, changing, testing across restart, or promoting a plugin."
---

# Develop a plugin (L3)

Developing a plugin is **L3** of the four-layer modification model (see the
`meiva-guide.modification-layers` skill): it happens entirely inside your deployment,
gated by the deployment owner. It does **not** ride the kernel pipeline — no
worktree/PR/CI against the kernel repo, no fleet update. Design record:
`docs/decisions/extensions/skills/2026-08-19-four-layer-modification-model.md`.

**Plugins live in their own repos** (user ruling, issue #42). The builtin
plugins under `<repo>/meiva_builtins/plugins/` are a kernel-shipped base set —
like in-tree drivers — and change via L4; everything new starts external.

## The ladder

```
write locally  ->  test at your own restart  ->  when stable: its own git repo
               ->  install/enable on the machines that need it  ->  maintain
                   per-plugin (upgrade/rollback), never via cluster update
```

### 1. Write locally (no git needed)

An external plugin is a directory `~/.meiva/plugins/<name>/` with a `plugin.py`
(entry point; hooks / SDK namespaces / services are declared from
here), an `meiva-plugin.json` manifest (required: identity, `version`, an
`engines.meiva` range, and the `contributions` it declares, which must match what
its `contribute()` returns; a plugin without one is refused at load), and optionally `setup.py` (idempotent `scaffold(*, machine: MachineIdentity)`, run by explicit
`meiva memory init` with its invocation-owned machine),
`skills/` (skill dirs converge syncs into the load dir), and `default_config.py` (the config class, declared by its own `contribute()` returning `PluginContributions(config=Cls)`).
Plugin discovery is a filesystem scan — a hand-placed directory is found; no
registration step is needed to develop. Look at a builtin under
`<repo>/meiva_builtins/plugins/` (e.g. `meiva_code`) for the shape, and at
`docs/conventions/extensions/plugin-spec-v2.md` for the contribution-surface vocabulary.

Enable it on this machine: `meiva plugins enable <name>` (writes the per-machine
`plugins_config.json`).

### 2. Test at the restart boundary

**The reload boundary is the agent process's `self.restart`** (user ruling
2026-08-13, recorded in `docs/conventions/extensions/plugin-spec-v2.md`). There is no
in-process hot reload: after editing plugin code, restart the affected agent process
(internal agents can use `meiva.self.restart(...)`; operators use the documented
agent restart command) and the fresh process loads the new code. Canary it
on yourself first; today enablement is per-machine, so every agent on the
machine loads an enabled plugin — per-agent activation (enable for ONE agent,
widen later) is designed in issue #39 and not built yet. A plugin that fails
at load time therefore hits every agent on the machine: keep the module
import + `plugin.py` entry cheap and fail-fast.

### 3. Promote to its own repo when stable

Create a git repo for it — public or local; a local bare/plain repo is enough
(`git init`, commit). Keep the `meiva-plugin.json` manifest at the package root
(`docs/conventions/extensions/plugin-spec-v2.md`): identity, version, `engines.meiva` range,
dependencies. The manifest is validated at install time where the install
flow supports the package kind.

**Current gap, stated honestly:** `meiva plugins install <url>` today recognizes
bare-skill packages and Claude Code plugin packages; a native `plugin.py`
package is not yet installable through it (the manifest-driven native install
is plugin-spec-v2 S3+ work). Until that lands, the durable install of a native
plugin is: clone its repo to `~/.meiva/plugins/<name>` on each machine that
should run it, then `meiva plugins enable <name>`. Upgrade = `git -C
~/.meiva/plugins/<name> pull` + restart the agents; rollback = `git checkout
<tag>` + restart. Third-party code installed this way bypasses the
supply-chain scan that `meiva plugins install` runs — read it in full first.

### 4. Maintain per-plugin

Version, upgrade, and roll back the plugin against **its own repo**, at the
restart boundary. The fleet update never touches external plugins — it is
the L4 kernel rollout. Disable is `meiva plugins disable <name>`; removal of a
registry-tracked install is `meiva plugins uninstall <name>`.

## What this is not

- Not for changing a **builtin** plugin — that is a kernel change (L4, the
  `meiva-self-development` workflow in the kernel repo).
- Not a way around the restart boundary — do not design for in-process reload;
  if something looks like it needs one, the answer is a restart.
- Not an MCP server package — a standalone MCP installs with `meiva mcp install`
  and has its own lifecycle.
