# .agents/skills — project skills (open Agent Skills standard)

This directory is the repo's project-skill root: skills written for Ava's own
development, following the [open Agent Skills standard](https://agentskills.io)
layout (each skill is a directory with its own `SKILL.md`).

**Distribution: project-local only for the 11 real project-skill directories.**
They are NOT a `converge` source (issue #146 /
`decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`).
An Ava agent sees them only through the project-local mount —
`ava_builtins/plugins/ava_code/_walk.py:project_skill_roots` resolves this
directory from `ava.cwd` at scan time — so they load exactly when the agent is
working inside this checkout, never on a machine that only runs Ava. This
project-local family covers repo-development workflow and Ava-cluster-operations
skills (`ship-a-change`, `write-a-pr-description`, `ava-self-development`, …).
General methodology and user-service skills are built-ins instead, then appear
here through mirrors for open-standard clients.

## Built-in mirrors are symlinks

The other 26 entries are **symlinks back to `ava_builtins/skills/<name>`** (git
tracks each link itself, mode 120000) so every built-in skill is also reachable
under the open-standard path for other clients (Claude Code, editors). On a
platform that checks out symlinks as plain files (`core.symlinks=false`, e.g.
Windows), those entries land as ordinary files; tools that enumerate this
directory as a plain filesystem then see files instead of skills — treat the
symlink entries as mirrors, not separate copies. (Ava's load dir is unaffected
either way: built-ins converge from `ava_builtins/skills/` directly.)

The 11 directories that are **not** symlinks are real project skills authored
here (one dir = one skill; edit them in place).
