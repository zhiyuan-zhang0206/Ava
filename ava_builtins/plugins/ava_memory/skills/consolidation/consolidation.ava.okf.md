---
type: doc
title: consolidation skill — Daily Consolidation
description: "Daily consolidation process for the shared memory pool (git repo), a sub-skill of ava.skills.ava_memory.consolidation: skills/scripts consolidate/steward/arbiter_merge 脚本一步 commit + push + refresh index; single-box self-managed, multi-host orchestrated by arbiter (resident machine, holds schedule) with per-machine steward creating PRs, then refresh + rebase after merge."
tags:
- extensions
- agent-instruction
---

# consolidation skill — Daily Consolidation

## What it is

`plugins/ava_memory/skills/consolidation/SKILL.md` (loaded name `ava.skills.ava_memory.consolidation`, a sub-skill of [[ava_builtins/plugins/ava_memory/skills/skills.ava.okf.md|ava_memory root skill]]): the consolidation process for the shared memory pool (`ava.memory.PATH`, a git repo). During the day agents write notes into the working tree; on trigger they are merged to keep the pool committed / pushed / searchable. git / gh / refresh operations are all wrapped in the `ava memory` CLI — the agent only runs one command per step.

## Role naming (2026-08-01)

- **Memory Arbiter** = the cluster-level administrator agent (the old "Memory Steward" name). It runs the arbiter flow: merge all machine PRs, resolve conflicts, curate, refresh.
- **per-machine Steward** = the local role on each machine. It runs `skills/scripts/steward.py`: local sync + PR creation. It never merges.
- CLI verb names are unchanged: `arbiter` == the Arbiter's command; `steward` == the per-machine Steward's command.

## Triggers — fire early and often

1. **Daily 03:00** scheduled full consolidate (Arbiter).
2. **Per-machine watch, every 1–5 minutes** (resident Steward or polled):
   >10 memory files changed, or pending diff >200 lines, or uncommitted changes
   older than ~30 min → commit + push + PR immediately, notify Arbiter.
3. **Arbiter merge watch**: merge ready machine PRs as they arrive.

A missed trigger leaves notes unsearchable; an extra trigger only costs a cheap
commit — when in doubt, trigger.

## Three Deployment Topologies

- **Single box** (default): sole consolidator, checkout directly tracks `main`; on first run `ava.watcher.cron("0 3 * * *", ...)` sets the schedule. In keep-local mode (`AVA_MEMORY_KEEP_LOCAL`) there is no remote, only commit + refresh local index.
- **Arbiter / arbiter** (multi-host, one resident machine — the Memory Arbiter agent): holds the schedule; each round spawns a per-machine Steward for each machine (or the machines run resident stewards), waits for PR readiness, `skills/scripts/arbiter_merge.py` squash-merges each one, `ava memory refresh` rebuilds the index, notifies stewards to rebase, re-curates `MEMORY.md` (16000 character cap ≈ 200 lines index (Claude Code auto-memory scale), enforced by pre-commit hook).
- **per-machine Steward** (multi-host, one per machine): `skills/scripts/steward.py` one-step commit + push + create PR, reports to the Arbiter then idles (or stays resident as a scheduler with the 1–5 min trigger watch), upon receiving "rebase now" rebases and terminates.

## Content Discipline

Topic directories are shared, `machines/<name>/` holds per-machine notes; `MEMORY.md` is a curated index injected into all agents each session. Large content (handoff / logs / binaries) does not go into the pool; move to Vault and leave pointer notes.

## Key Dependencies

- [[ava_builtins/plugins/ava_memory/skills/skills.ava.okf.md|Ava Memory Skills]] — the root skill (Memory Arbiter manual) itself
- [[ava_builtins/plugins/ava_memory/memory-api.ava.okf.md]] — the `ava.memory` pool itself being operated on
- [[memory_indexer.ava.okf.md]] — the re-embedding service triggered by `ava memory refresh`
