---
type: doc
title: worktree skill — Isolated Workspace
description: Why coding changes should go into a git worktree named with the agent id, and when it's unnecessary — read before starting to modify the repo.
tags:
- extensions
- agent-instruction
---

# worktree skill — Isolated Workspace

`ava.skills.ava_code.worktree`: explains why coding changes go into a git worktree named after the agent id (parallel agents don't trample the main checkout, prod processes run on the main repo), how to create and tear it down, and what changes don't warrant the overhead. Belongs to the scenario deep-dives of [[ava_builtins/plugins/ava_code/skills/skills.ava.okf.md|ava_code carried skills]], loaded before starting repo changes.
