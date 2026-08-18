# .agents/skills — project skills (open Agent Skills standard)

This directory is the repo's project-skill root: skills written for Ava's own
development, following the [open Agent Skills standard](https://agentskills.io)
layout (each skill is a directory with its own `SKILL.md`). It is one of the
two repo-native skill sources `converge` syncs (the other is
`ava_builtins/skills/` for built-ins) into `$AVA_HOME/skills/`.

## Built-in mirrors are symlinks

Most entries here are **symlinks back to `ava_builtins/skills/<name>`** (git
tracks the link itself, mode 120000) so every built-in skill is also reachable
under the open-standard path. On a platform that checks out symlinks as plain
files (`core.symlinks=false`, e.g. Windows), those entries land as ordinary
files and are **skipped** by `iter_sources` (a link is not a source tree) —
built-ins then converge from `ava_builtins/skills/` directly, so the
degradation is invisible to the load dir. Tools that enumerate this directory
as a plain filesystem (Claude Code, editors) see files instead of skills on
such platforms; treat the symlink entries as mirrors, not separate copies.

Directories that are **not** symlinks are real project skills authored here
(one dir = one skill; edit them in place).
