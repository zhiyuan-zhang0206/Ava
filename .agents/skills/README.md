# .agents/skills — project skills (open Agent Skills standard)

This directory is the repo's project-skill root: skills written for Ava's own
development, following the [open Agent Skills standard](https://agentskills.io)
layout (each skill is a directory with its own `SKILL.md`).

**Distribution: project-local only.** These skills are NOT a `converge` source
(issue #146 / `decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`).
An Ava agent sees them only through the project-local mount —
`ava_builtins/plugins/ava_code/_walk.py:project_skill_roots` resolves this
directory from `ava.cwd` at scan time — so they load exactly when the agent is
working inside this checkout, never on a machine that only runs Ava. The
kernel-contributor family (`ship-a-change`, `write-a-pr-description`,
`ava-self-development`, …) is L4-only content a runtime agent will never act
on, which is why it stopped being fleet-distributed.

## Built-in mirrors are symlinks

Most entries here are **symlinks back to `ava_builtins/skills/<name>`** (git
tracks the link itself, mode 120000) so every built-in skill is also reachable
under the open-standard path for other clients (Claude Code, editors). On a
platform that checks out symlinks as plain files (`core.symlinks=false`, e.g.
Windows), those entries land as ordinary files; tools that enumerate this
directory as a plain filesystem then see files instead of skills — treat the
symlink entries as mirrors, not separate copies. (Ava's load dir is unaffected
either way: built-ins converge from `ava_builtins/skills/` directly.)

Directories that are **not** symlinks are real project skills authored here
(one dir = one skill; edit them in place).
