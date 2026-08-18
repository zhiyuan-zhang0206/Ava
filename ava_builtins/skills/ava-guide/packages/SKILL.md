---
name: packages
description: Install, upgrade, and remove skills & plugins — the package management surface, plus the skill vs plugin distinction.
---

# Packages — Skills & Plugins

You extend yourself by handing a git source to the CLI. It clones the source,
places the package in your overlay, and records where it came from so you can
upgrade or remove it later. Two package shapes install today:

- a **skill** — a git repo (or subdir) whose root holds a `SKILL.md`.
- a **Claude Code plugin** — a directory holding `.claude-plugin/plugin.json`
  that bundles an `agents/` set and/or a root `.mcp.json`.

A skill takes effect on the next skill scan; a bundled MCP server connects the
next time you use it — either way **no restart needed**.

This skill is the CLI reference. When the job is "the user wants a capability,
find something and get it working", read
`ava.help(ava.skills.ava_package_installer)` instead — it owns the whole flow
(find candidates, confirm, install, verify with a test agent, judge).

## Skill (Reusable Instruction Pack)

A **skill** is a reusable instruction pack — you read and follow it, it is not
code. Use `ava.help(ava.skills.<name>)` to load it. All skills form a namespace
tree mirroring the folder structure (top-level bare, plugin-bundled under
`plugin.name`, nested as `a.b.name`). Python access replaces hyphens with
underscores (e.g. `ava.skills.superpowers.test_driven_development`).

A skill directory structure: root `SKILL.md` (frontmatter must have `name` +
`description`), plus optional sub-directories with their own `SKILL.md` for
deeper detail.

## Plugin (Extension Mechanism)

A **plugin** is heavier than a skill — it carries Python code: **hooks** that
plug into the agent execution graph (`before_llm` / `before_exec` / `after_exec`),
bundled **MCP servers** (via `.mcp.json`), sub-**agent** collections, and its own
config schema.

The distinction: a skill is instructions an agent reads and follows; a plugin
injects code into the runtime itself.

## Skill lifecycle — two modes (R5 design)

Every skill in the load dir (`$AVA_HOME/skills/`) is in exactly one of two
modes. Know which one yours is in — the two are governed differently.

### Mode 1 — experiment (untracked, not updatable)

Write a skill directory directly under `$AVA_HOME/skills/<name>/` and register
it so the scanner loads it:

```bash
ava skill register <name>
```

It loads immediately. There is no source and **no update path** — the update
mechanisms skip it by design. This is the right shape for a quick local test
of an idea, nothing more.

### Mode 2 — canonical (a git repo path)

A real skill lives **in a git repo**: a local git repo, a remote private repo
(such as a private skills repo), or a public one. The repo is the single
source of truth for the skill's content, versions, and updates (a version is
a git ref — tag / commit / branch). Install records the source:

```bash
ava skill install <git-url | local-path> [--path <subdir>] [--ref <tag | commit | branch>]
```

The recommended workflow:

1. **Experiment** — iterate on the files directly (the load-dir copy is the
   test bed; `ava skill register` if it is not installed yet).
2. **Commit** — once the skill works, commit it into its repo. From that
   moment the repo (not the load dir) is the source of truth.
3. **Install / update through the command** — the load-dir copy becomes
   derived state:

   ```bash
   ava skill install <repo> --path skills/<name>   # first install
   ava skill upgrade <name>                        # re-fetch from the recorded source
   ```

   A locally edited load-dir copy blocks `upgrade` with a conflict — re-run
   with `--force` to overwrite your edits (mirrors `git pull` vs
   `reset --hard`).

Rules that keep the model clean:

- A hand-written Mode-1 skill is not updatable by design; to make it
  updatable, put it in a repo and install it from there.
- Repo-native skills (those shipped by the Ava checkout) are never updated by
  converge — `ava skill update` is the explicit update command for them.
- There is no separate manifest file to maintain: `SKILL.md` frontmatter
  (`name` + `description`) is the identity, and the git ref is the version.

## Install

```bash
ava plugins install <git-url> [--path <subdir>] [--ref <tag | commit | branch>]
```

The source may be a **git URL or a local directory** — a local git repo works
as a source too (install reads it in place; `upgrade` re-reads it). `--path`
selects a subdirectory (Claude Code plugins usually live at
`plugins/<name>/` inside a monorepo). `--ref` pins a version; omit to track
default branch. Confirm visibility with `ava.help(ava.skills)` — no restart.

Example:

```bash
ava plugins install https://github.com/anthropics/claude-plugins-public --path plugins/pr-review-toolkit
```

## List / Upgrade / Remove

```bash
ava plugins installed                 # each installed package: name, enabled state, source
ava plugins upgrade <name> [--force]  # re-fetch from recorded source at pinned ref
ava plugins uninstall <name>          # delete installed copy + registry entry
```

Skill packages use the same machinery with their own verbs:

```bash
ava skill install <src> [--path <subdir>] [--ref <ref>]  # user install (Mode 2)
ava skill update [name ...] [--force]   # repo-native skills: sync from this checkout
ava skill upgrade <name> [--force]      # user-installed skill: re-fetch recorded source
ava skill register <name>               # Mode-1 untracked skill: make it load
```

**Every update path shares one conflict contract** (R5): a locally edited copy
refuses to be overwritten; `--force` is the explicit override. A skill with no
recorded source (Mode 1) reports "not updatable" instead of guessing.

## Enable / Disable

```bash
ava plugins enable <name>     # local-config toggle (this machine)
ava plugins disable <name>
```

Enable/disable is a local-config toggle — does not remove the package, just
controls whether it loads on this machine.

## Plugin Config Schema Reconcile

A plugin ships a config class; the persisted disk image can drift after upgrade.
Run `ava plugins update` to align the persisted config to the new schema
(fill defaults, keep deprecated fields, reject on type incompatibility).

## Notes

- A skill package needs a `SKILL.md` at the package root; a Claude Code plugin
  needs `.claude-plugin/plugin.json` plus at least one of an `agents/` set or a
  root `.mcp.json`. A plugin that bundles neither (skills-only / hooks-only) is
  **refused** until those pieces land.
- Install only from a source you trust: the package's files run in your own
  environment.
- If a clone or checkout fails, the command prints git's error — read it, fix
  the URL / ref, and retry.
