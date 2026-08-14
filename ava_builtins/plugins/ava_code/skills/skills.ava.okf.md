---
type: doc
title: Ava Code Skills — plugin-carried skills
description: "The 4 on-demand deep-dive skills carried by the ava_code plugin: conventions / pr / testing / worktree. The system prompt only injects coding convention stubs; details live in skills — agents load them in the corresponding scenarios (before commit, before opening a PR, before modifying the repo, before declaring completion)."
tags:
- extensions
- agent-instruction
---

# Ava Code Skills — plugin-carried skills

## What It Is

`ava_code`'s system prompt injection only places stub-level coding conventions; deep-dive details live in the 4 skills carried by the plugin (`plugins/ava_code/skills/`, converged to `~/.agents/skills/ava_code/`, load name `ava.skills.ava_code.<name>`), which agents load via `ava.help` when entering the corresponding scenario:

- [[ava_builtins/plugins/ava_code/skills/conventions/conventions.ava.okf.md|conventions]] — AGENTS.md auto-injection mechanism deep dive + shell-vs-files tool selection
- [[ava_builtins/plugins/ava_code/skills/pr/pr.ava.okf.md|pr]] — pre-push self-check, PR creation and verification, commit-trailer, repo hygiene
- [[ava_builtins/plugins/ava_code/skills/testing/testing.ava.okf.md|testing]] — complete check set + criteria for whether new tests are needed
- [[ava_builtins/plugins/ava_code/skills/worktree/worktree.ava.okf.md|worktree]] — why (and when not) to modify code in an agent-id-named worktree

## Notes

For the skill mechanism and origin axis (origin=plugin), see [[ava/skills.ava.okf.md|Skill System]]; for the plugin's prompt/hook injection surface, see [[ava_builtins/plugins/ava_code/ava_code.ava.okf.md|ava_code plugin]].
