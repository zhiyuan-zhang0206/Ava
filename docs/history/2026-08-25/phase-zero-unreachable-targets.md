# Phase-zero unreachable targets

During the 2026-08-25 production rollout, WSL's Phase 0 fetch timed out and was
reported as skipped. The host became reachable before Phase A, so the unchanged
fan-out list paused it anyway. Phase B then failed migration-layout validation
because the pinned commit object had not been proven present, leaving WSL paused
and off-pin while the other hosts converged.

The rollout target set is now frozen after Phase 0. A runner with an unreachable
fetch is excluded from pause, update, and compensating resume for that rollout;
it converges at the next rollout, or when `ava cluster update` runs on that host.
Re-admitting a
host merely because a later ops request answers was rejected: ops reachability
does not establish the validate-before-kill precondition that the pinned Git
object exists locally.

The regression test drives the real orchestration with a Phase 0 timeout followed
by later-phase success responses and requires that the skipped host is never
dialed by those later phases.
