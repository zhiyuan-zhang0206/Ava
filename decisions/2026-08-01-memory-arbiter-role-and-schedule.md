# Memory Arbiter — role rename, consolidation triggers, schedule template (2026-08-01)

## Decision
1. The cluster-level memory administrator role is renamed from **Memory Steward**
   to **Memory Arbiter** (user-directed): it matches the CLI verb
   `ava memory arbiter merge`, so role name and command name agree. The word
   "Steward" now means only the **per-machine Steward** (local sync role).
2. Consolidation triggers (principle: fire early and often — a missed trigger
   leaves notes unsearchable; an extra trigger only costs a cheap commit):
   - daily 03:00 full consolidate (merge all machine PRs + curate + refresh);
   - per-machine watch every 1–5 min: >10 memory files changed, or pending diff
     >200 lines, or uncommitted changes >30 min old → commit+push+PR immediately;
   - arbiter merge watch: merge ready machine PRs as they arrive.
3. The schedule script is version-controlled: template at
   `docs/schedules/memory-arbiter-schedule.py` (deployed via
   `ava schedules update 2 --script-file ...`); the running copy at
   `~/.ava/schedules/2/` is a deployment artifact, not the source of truth.
4. Supersedes the 2026-07-11 Memory Steward role record (label: memory-arbiter,
   schedule id=2 name: memory-arbiter; 12PM PR-merge trigger was removed 2026-08-01
   after the Race Round 3 incident — ava-dev merges belong to the merge captain).

## Date
2026-08-01
