---
type: doc
title: Start Readiness Gate & Verdict
description: ava start's readiness gate (_wait_for_services_ready, the 0/1/4 exit contract, --no-readiness-gate callers, the waiver flag-arrival rule, and the record that survives the waiver).
tags:
- cli
- start
- readiness
---

# Start Readiness Gate & Verdict

- `cli/commands/_probe.py:_wait_for_services_ready` is `ava start`'s **readiness gate**: it polls
  the probes `ava status` uses for the roster the start just launched and returns the
  specs that never passed, which `start.py` turns into
  `SERVICES_NOT_READY_EXIT_CODE` (4) *after* the status snapshot has printed and the
  sessions are named. Three start outcomes, then — 0 serving / 4 up but incomplete /
  1 a step failed — because a human reads the crosses and a program reads `rc`. The
  gated set is not a list: it is `ops.spec`'s capability view minus `_gate_reason`
  skips, minus `--disable-service`, minus the frontend (its ~30-60 s build would set
  the floor for every start), so *skipped* and *unready* stay different answers. The
  bound is `shared.deploy_timing.SERVICE_READY_TIMEOUT_S`; a service whose session
  has died ends the wait at once rather than spending it. The wait returns a
  `ReadinessWait` — the unready specs plus the elapsed time and *which* of those two
  exits it took — because the printed verdict has to say which one: the bound is a
  meaningful number only when it was actually spent, and naming it after a
  sessions-gone exit states an elapsed time the surrounding timestamps contradict.

  **The gate is tiered** (C2, Task #2183): `_probe.CRITICAL_SERVICE_SESSIONS`
  (gateway / frontend / restarter / agent-host — the hosted agent-runner) is the
  only roster that can fail a start and the only one that gets the full 180 s
  bound. Every other launched service gets
  `shared.deploy_timing.NON_CRITICAL_SERVICE_READY_TIMEOUT_S` (45 s) — the
  2026-08-30 rollout spent 182 s of its 197.5 s local start on a pitr-uploader
  whose verdict nothing downstream depended on. A non-critical service that
  misses its window (or whose session is confirmed gone) leaves the wait and
  lands in `ReadinessWait.non_critical_unready`: `start.py` prints it and posts
  an alert (`_probe._notify_non_critical_unready_services` — the `alerts` store +
  IM, the same channel the health probes use), so the demotion is a verdict
  change, never a silence.
- Two callers pass `--no-readiness-gate`, which keeps the wait and the printed
  crosses but drops the exit code. The **boot job** on all three platforms
  (`cli/boot_retry.py` + the `shared/os_autostart.py` plist), because its retry has
  no attempt cap and an unbounded retry over a permanently-unready service is a host
  that never boots — and because launchd's `SuccessfulExit` is a boolean, so only
  opting out on every platform keeps the one behaviour `shared/boot_policy.py`
  requires them to share. And the **rollout's local gateway leg**, whose readiness
  question `_gateway_ready` answers one step later and better.
- A flag only arrives if the caller knows to pass it, and the rollout's local gateway
  leg is spawned by an orchestrator that imported `update.py` *before* the checkout —
  so on the rollout that first ships a flag change, the parent is old code and the
  child is new. `cli/commands/start.py:_readiness_waiver` therefore also waives the verdict when a
  gateway-capable host *observes* a live update lease (`cli/commands/status.py:_update_in_flight`),
  which is the same fact reached without the parent's cooperation. Scoped to gateway
  hosts: only that leg's exit code becomes a cluster-wide revert, while a pure
  agent-runner's 4 buys an idempotent local `ava start`.
- Waiving the *code* never waives the *record*. The rollout reads
  `shared/launch_failures.py` after its local leg and, on a non-empty list, downgrades
  the whole rollout to `RolloutOutcome.INCOMPLETE` (rc 1) with the sessions named in
  the aftermath block — without aborting it: the gateway is serving and the
  agent-runners still need Phase B, so the failure changes the verdict, not the plan.
  The frontend-only fast path returns before that machinery exists, so it prints its
  own block and returns 1 in `cli/commands/update.py:_run_frontend_only_update`.


Parent: [[cli/commands/start-readiness/start-readiness.ava.okf.md|start readiness]].
