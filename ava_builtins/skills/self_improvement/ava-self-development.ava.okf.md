---
type: doc
title: ava-self-development skill — Modify own code and make it effective
description: The manual for the only effective path for Ava to modify its own code — PR → CI → merge → `ava cluster update` rollout across the cluster; the core is the counterintuitive discipline of "no in-process shortcuts", can't edit running source code then reload.
tags:
- extensions
- agent-instruction
---

# ava-self-development skill — Modify own code and make it effective

## What it is
Ava is an agent that can modify its own code, but "how to make changes effective" is a counterintuitive path — this skill (`$AVA_HOME/skills/ava-self-development/`) nails down that path. Its reason for existence is to block a fatal intuition: **editing source code loaded by a running process, then `importlib.reload` to "verify" the change**. That doesn't work (the process keeps already-imported modules), and when hot-reloading a live SDK, it can corrupt in-flight state or even crash the process. **There is no in-process shortcut**.

## The only effective path
1. Modify in a **development workspace** (a separate clone / worktree), never in the production checkout `~/.ava/source` — that is the tree where live processes boot; touching it (editing / `git checkout` / creating branches) will cause newly spawned agents across the cluster to start on unreviewed code, and the next update force-checkout will discard those commits.
2. Open a PR, let CI run — CI is the only trust point.
3. Human review and merge to `main` (according to collaboration discipline, the user decides the merge).
4. Run `ava cluster update` (the CLI — the only update entry point since
   `ava.self.update()` was removed 2026-08) — roll the merged `main` across the
   entire cluster (each agent restarts to the new code). Only at this step do
   changes become live. The main body of the skill text is **step 4** (rollout
   mental model + ops details).

## Key dependencies
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-improvement & evolution skill]] — belongs to functional group
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and origin axis
- [[ava/self.ava.okf.md|ava.self]] — the rollout architecture (the SDK `update()` that once triggered it was removed 2026-08; the CLI `ava cluster update` is the only entry point)
- [[ava_builtins/plugins/ava_code/ava_code.ava.okf.md|AvaCode]] — worktree + PR mechanics (how changes move in one's own clone)
