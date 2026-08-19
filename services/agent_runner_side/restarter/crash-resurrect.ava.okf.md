---
type: doc
title: CrashResurrectController
description: The 30s auto-resurrect scan — criteria (involuntary death + pending inbound + backoff), the never-resurrect set, the enforced termination_source stamp, and the gateway health gate.
tags:
- services
- restarter
- resurrect
---

# CrashResurrectController

## CrashResurrectController (`ops/controllers/resurrect.py`) — Auto-Resurrect on Crash
- **Criteria**: local `terminated` agents with `termination_source ∈ {'reaper','launch-confirm'}` (involuntary death), pending inbound in the workload allowlist (`chat`/`compact_request`), past per-agent backoff (`auto_resurrect_backoff_seconds`; one UPDATE atomically claims + stamps `last_resurrect_at`) → `ops.agents.resurrect_agent`.
- **Never resurrect**: `'user'` (force-kill) / `'exit'` (self-exit) — explicit intent; `'integrity'` (row state self-inconsistent — ops looks first); NULL (pre-column legacy) — only post-change rows eligible, rollout-safe.
- **The allowlist is the whole safety model**, so the stamp is enforced, not just documented: an unstamped write leaves NULL, which this scan can never claim — the queued inbound strands silently. `scripts/lint_termination_source.py` fails any terminated-write that omits its source; `TerminationSource.resurrectable()` is what `_RESURRECTABLE_SOURCES` reads. `'launch-confirm'` also covers the child's early-boot schema/placement rejection (`agent/_starting.py`).
- **Why needed**: Gateway's `resurrect_if_terminated` fires only on new inbound; already-queued inbound after a crash gets no wake event → indefinite stall. This controller is the persistent fallback.
- **Gateway health gate**: same as RespawnController; unhealthy → postpone round (no backoff stamp, retry next round).
- 30s cadence; a single resurrection failure is only logged, the round continues.


Parent: [[services/agent_runner_side/restarter/restarter.ava.okf.md|restarter]].
