---
type: doc
title: Pre-push frontend selection
description: "How the frontend pre-push hooks select work from the branch contribution: the merge-base range, the disk and type inputs outside Vitest's import graph, and the fixed repo-local commands they run."
tags:
- scripts
- ci
---

# Pre-push frontend selection

`scripts/provision/prepush_frontend.py` runs the frontend checks over the
branch contribution, never an old push tip. Vitest's import graph is only one
source of consumers: existing filesystem contract tests stay explicit, while
global configuration and deleted-module closure belong to CI and are reported
as unverified locally rather than running a full suite.

Every process this selector runs is a fixed repo-local command — git queries and scripts/hooks/prepush-*.sh — with repo-owned path arguments and never a shell, so no untrusted input reaches argv (S603 is exempted for this file in pyproject.toml on that ground). The heavy-tool invocation is deliberately unbounded — the developer watches it run — and prepush-guard.sh serializes one heavy tool across host worktrees and reports skips loudly instead of failing silently.
