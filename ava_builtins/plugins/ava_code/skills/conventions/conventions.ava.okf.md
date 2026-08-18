---
type: doc
title: conventions skill — Context Files and Tool Selection
description: Deep dive into the AGENTS.md auto-injection mechanism + tool selection criteria for shell-vs-files — read when AGENTS.md doesn't surface, or when unsure which tool to use.
tags:
- extensions
- agent-instruction
---

# conventions skill — Context Files and Tool Selection

`ava.skills.ava_code.conventions`: explains how the [[ava_builtins/plugins/ava_code/ava_code.ava.okf.md|ava_code plugin]]'s AGENTS.md / CLAUDE.md auto-injection mechanism (after_exec hook collects upward along read paths, system note injection, dedup) appears from the agent's perspective, how to troubleshoot when injection doesn't happen, and the selection criteria for `ava.shell` vs `ava.files`. An on-demand deep-dive manual — the main-path behavior is already covered by the plugin's system prompt stub.
