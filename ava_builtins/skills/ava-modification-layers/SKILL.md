---
name: ava-modification-layers
description: Pick the right layer before changing an Ava deployment — L1 install extensions, L2 edit skills, L3 develop plugins, L4 change the kernel. Each layer has its own apply mechanism; only L4 goes through `ava cluster update`. Never treat the prod checkout as a workspace.
---

# Modification layers

You (and your user) modify an Ava deployment at four distinct layers. Each has
its own medium, its own apply mechanism, and its own gate-holder. Picking the
wrong layer wastes the heavy machinery of a higher one — or, worse, applies an
unreviewed change through a shortcut that does not exist. The design record is
`decisions/2026-08-19-four-layer-modification-model.md`.

| Layer | You change | Applies via | Gate-holder |
|---|---|---|---|
| **L1 — install** | Add an existing extension: `ava plugins install <url>`, `ava skill install <url>`, `ava mcp install <url>` | Next skill scan / next use — no restart | Deployment owner (install-time supply-chain scan) |
| **L2 — skill edit** | A SKILL.md under `$AVA_HOME/skills/` (or a repo/plugin source synced there) | Immediately at the next invocation — skill bodies are read fresh (mtime-cached) each time a skill is loaded | The agent / deployment owner |
| **L3 — plugin development** | A plugin package (its own repo or `~/.ava/plugins/<name>/`) | The agent process's `self.restart` boundary — no in-process hot reload (user ruling 2026-08-13) | Deployment owner |
| **L4 — kernel change** | The Ava kernel repo itself | PR → CI → human merge → `ava cluster update` | Upstream maintainer + CI |

`ava cluster update` belongs to **L4 only** (plus routine version tracking).
If your change never touches the kernel repo, no PR pipeline and no cluster
rollout is involved.

## Routing

- **L1** — the `ava-package-installer` skill (verified install of skills /
  plugins / MCP servers).
- **L2** — edit the SKILL.md; your next invocation of the skill reads the new
  body. Two catch-up delays are deliberate: the system-prompt **index**
  (name + description lines) is a snapshot rebuilt only at compact/spawn, and
  **other live agents** are not notified that the catalog changed — their
  prompts also catch up at their next compact/spawn. There is no broadcast
  mechanism yet (planned to ride the issue #39 skill-sync event). Skills that
  ship inside the kernel repo (`ava_builtins/skills/`) are the exception:
  durable edits to them are kernel changes — L4.
- **L3** — the `develop-a-plugin` skill: write locally, apply at your own
  restart, promote to its own git repo when stable. Builtin plugins
  (`ava_builtins/plugins/`) are a kernel-shipped base set and change via L4.
- **L4** — file an issue or a PR against the kernel repo. Contributors read
  the `ava-self-development` skill (the kernel-contributor manual: worktree →
  PR → CI → merge → `ava cluster update`).

## Safety: the production checkout is not a workspace

> ⚠️ `~/.ava/source` (`$AVA_HOME/source`) is the tree the live cluster boots
> from — it is not your working copy. A stray edit, `git checkout`, or
> `git branch` there breaks every new agent spawn fleet-wide (agents start
> from on-disk code, not memory), and the next `ava cluster update`
> force-checks-out the target and discards stray commits. To change kernel
> code, work in a separate clone/worktree like any other contributor.

Equally: there is **no in-process shortcut** at any layer. Editing source that
a running process already imported and calling `importlib.reload(...)` does
not take effect and can corrupt in-flight state. L2 applies at the next skill
invocation, L3 at `self.restart`, L4 at the rollout — nothing applies by
reloading a live module.

## When something is broken at a lower layer than you can fix

A kernel bug found while working at L1–L3 escalates to L4: open an issue (or a
PR) against the upstream kernel repo. Do not patch the prod checkout, do not
fork the behavior inside a plugin when the kernel contract itself is wrong.
