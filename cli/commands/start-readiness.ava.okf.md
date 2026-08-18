---
type: doc
title: Start Readiness (what `ava start` calls up)
description: The questions `ava start` asks about a service and the one exit code they produce — `_session_lifecycle`'s launch guard (is this live session actually serving?) and its launch verdict (did `new-session` even take?), `_probe.py:_wait_for_services_ready` (did what I launched come up?), and `start.py:_readiness_waiver` (is an unready service this start's fault?). Split from the parent node; the launch guard and the wait share one frontend carve-out on purpose.
tags:
- cli
- tool
- lifecycle
---

# Start Readiness (what `ava start` calls up)

## What it is

`ava start` decides twice whether a service is running: once **before** launching
(skip what is already up) and once **after** (did the roster come up?). Both used to
be answered by cheaper questions than the ones they meant, and the pair composed into
a start that skipped a dead daemon and printed `✓`
([decision](../../decisions/2026-07-31-a-live-session-is-not-a-running-service.md)).

Before either of those, it asks a third question that neither can answer after the
fact: **is the port I am about to bind mine to bind?**

## Notes

- `start.py:_refuse_occupied_health_ports` is the **pre-bind gate**, run on the roster
  `_session_lifecycle._launch_roster` reports (the same set the launch uses, so a gated-out or
  `--disable-service`-d daemon's port is not one this start would take). For each spec
  whose probe target is an Ava `/healthz` it runs `_probe._occupied_health_ports`,
  which refuses only on a **terminal** verdict — this unit's own daemon is `ALIVE`, a
  stray of its own home and an empty port are `DOWN`, so an idempotent restart and a
  cold host both pass, and only a port held by something no respawn of ours can
  dislodge stops the start (rc 1, nothing launched). It exists because `home` is the
  identity of a UNIT: two agent-runners of one cluster on one machine are handed the
  same ports by construction, and WSL2's loopback relay makes one of them answer the
  other's probe — after which the watchdog reads green while the daemon it is
  supervising is gone ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]],
  [decision](../../decisions/2026-07-31-a-health-port-belongs-to-a-unit.md)). The
  readiness gate cannot substitute: by the time it runs, the sessions are up and the
  relayed port is already answering for someone else.
- `_session_lifecycle._launch_sessions`' idempotence guard asks **the service, not the
  session**: it skips a spec only when the session exists *and*
  `_probe._husk_session_reason` finds nothing wrong, otherwise it clears the session
  and launches. The backend keeps the session, not the daemon, so a session whose
  service died is a session that exists with nothing behind it — and a kill that reported success it
  never confirmed is how one gets left there. The frontend is exempt from the check by
  `_probe._probe_judges_a_fresh_launch`, the same predicate that keeps it out of the
  readiness wait, because mid-`npm run build` is not the same state as dead. A husk
  the clearing kill cannot remove is reported and left for the readiness gate rather
  than handed to `new-session`, which would only fail on the duplicate name — the
  session is still there and the husk verdict came from a single probe, so calling it
  a failed launch would fail starts whose service was alive throughout.
- A `new-session` that is **refused** is the one failure no probe can diagnose: nothing
  was spawned, so waiting cannot make it appear. `_launch_sessions` retries it
  once (clearing the name first, so the retry cannot fail on the duplicate instead) and
  returns whatever still will not start in `LaunchOutcome.failed`; before that the bool
  was dropped and a refusal reached no caller and no exit code — prod's 2026-08-06
  frontend-only rollout printed `✗ failed to start ava-frontend` and
  `rc=0` in the same block. `start.py` names the survivors, folds them into the same
  verdict as the unready ones (same operator move, and the two callers that waive it
  have better channels), and writes them to `$AVA_HOME/last_launch_failures`
  (`shared/launch_failures.py`). That record exists because the rollout runs this start
  in a *child*: an exit code cannot carry names, and the parent's own roster is the
  pre-pull tree's, so only the child can say what it failed to launch.
- `_probe.py:_wait_for_services_ready` is `ava start`'s **readiness gate**: it polls
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
  child is new. `start.py:_readiness_waiver` therefore also waives the verdict when a
  gateway-capable host *observes* a live update lease (`status.py:_update_in_flight`),
  which is the same fact reached without the parent's cooperation. Scoped to gateway
  hosts: only that leg's exit code becomes a cluster-wide revert, while a pure
  agent-runner's 4 buys an idempotent local `ava start`.
- Waiving the *code* never waives the *record*. The rollout reads
  `shared/launch_failures.py` after its local leg and, on a non-empty list, downgrades
  the whole rollout to `RolloutOutcome.INCOMPLETE` (rc 1) with the sessions named in
  the aftermath block — without aborting it: the gateway is serving and the
  agent-runners still need Phase B, so the failure changes the verdict, not the plan.
  The frontend-only fast path returns before that machinery exists, so it prints its
  own block and returns 1 in `update.py:_run_frontend_only_update`.

## Key Dependencies

- [[commands.ava.okf.md]] — the command-module overview this node splits from
- [[shared/machine.ava.okf.md]] — `ServiceSpec.capabilities`, whose per-host union is
  the roster both the launch guard and the wait are handed
