# A hung rollout is interrupted, not killed

## Context

On 2026-08-02 03:15 PDT the v0.13.0 rollout — `ava update --local` inside the detached
`ava-rollout` session — blocked for 67 minutes inside `converge`, on a `codesign`
child waiting for a GUI authorization that no detached session can ever answer. Phase A
had already stopped the world, so the cluster sat down for the whole window.

Every self-healing layer stood down, and each of them was right to. `stranded_pause`
saw a live deploy lease and logged `cluster update holds the lock … not unpausing`. The
watchdog, resurrected by its OS-scheduled probe after the rollout's own `ava stop` took
it down, logged `round blocked by pause (scope=all)` once a minute for 67 minutes. The
health probe and `ava cluster recover` both refuse while the lock holder's pid is alive.
**Every one of them asks whether the owner is alive; none asks whether it is getting
anywhere**, and a process blocked forever on a prompt is maximally alive.

The recovery machinery itself was fine. The moment that step finally returned failure,
the in-process abort ran end to end — rolled back to last-known-good, brought services
up, resumed each machine, left the pin unmoved. The gap was purely that nothing would
ever *cause* that step to return.

`NO_PROGRESS_TIMEOUT_S` already existed as this repo's one definition of "stopped making
progress", and `ops/controllers/stalled_updater.py` already turned it into an unattended
reclaim — but only for `ava-updater`, a host's own self-update. The orchestration that
pauses the whole fleet was outside every clock in the system.

## Decision

A gateway watchdog controller (`ops/controllers/stalled_rollout.py`) reads the same
900 s no-progress bound against the rollout, and reclaims it in two stages.

**Evidence** is the `cluster_last_update` row read as `RUNNING` — an open row whose
recorded holder still holds the deploy lease — plus `max(started_at, rollout-log
mtime)`. Both marks are needed: the log mtime is the real progress signal, and
`started_at` is the floor that stops a *young* rollout from reading as an ancient one
(the log file is created by the pane's `tee`, not by `spawn_rollout`, so for a moment
after the session appears the newest `rollout-*.log` on disk is still the previous
run's). Every unreadable or ambiguous signal returns "not stalled".

**Stage 1 is `SIGINT` to the orchestration's own pid**, which the deploy-lease holder
string `<machine>:pid<N>` names directly, and only when that holder names this machine
and the pid is alive. Python turns `SIGINT` into `KeyboardInterrupt`, a `BaseException`
that no `except Exception:` in the orchestration swallows, so the `finally` in
`cli/commands/update.py` runs the recovery that was already there and already tested.

**`_run_gateway_local_update` now treats that interrupt as a failure rather than letting
it through.** That function's own block comment already stated the invariant — "the stop
above took the gateway down too, so ANY failure below leaves the gateway offline… each
failure recovers to last-known-good before returning non-zero" — and every non-zero
return honoured it while an exception did not. An interrupt that propagated would skip
the rollback and leave the gateway stopped on a half-applied transition: checkout moved,
migrations not run. That is worse than what a failed step produces, and it would make
this whole mechanism's claim ("a hang becomes a failure") false — it would become "a
hang becomes an abort". So the interrupt takes the same recovery branch and reports
through the same rc, which the orchestration already reads as recovered-vs-DOWN.

**Stage 2 is a session kill**, reached only after the interrupt has had a further
full no-progress window and changed nothing. Recovery then falls back to the path a
hard-killed rollout has always taken: the lease stops being renewed, lapses at
`LOCK_TTL_S`, and `stranded_pause` recovers a pause that has outlived its owner.

The controller is registered **ahead of `PauseController`**, the position
`stalled_updater` established. A rollout pauses this host in Phase A before it reaches
anything that can hang, so a hung rollout is only ever met on a paused host, and
`PauseController` blocks the round with `BlockScope.ALL`.

Separately, the Phase-B poll now writes a `still polling Phase B (Nm)` heartbeat on the
lease-renewal cadence. It was the one phase that could legitimately stay silent for
`_POLL_TIMEOUT_S` — the *same* constant this reclaim measures silence against — which
would have put a healthy slow fan-out exactly on the boundary.

## Alternatives rejected

**Kill the session, as the hung-updater reaper does.** The reaper's own docstring is
explicit that its narrow action is "kill one session that has been silent for 15
minutes", not "recover a host" — it hands the host back to the pause, pin and code
controllers that already own those dimensions. That reasoning does not carry across:
for a rollout, the recovery is not a controller, it is a `finally` **inside the process
being killed**. Killing first destroys the only thing that can unpause this host, resume
the remote agent-runners and close the `cluster_last_update` row, and leaves the cluster
exactly where the hang left it for up to a `LOCK_TTL_S`. That is why the kill is stage
two rather than stage one, not why it is absent.

**Kill only the hung leaf child** (the `codesign` process). Superficially the most
surgical option, and rejected on two counts. It cannot be found in general — a hang in
the orchestrator itself has no leaf — and it is unnecessary, because `subprocess.run`
kills its own child when `KeyboardInterrupt` unwinds through it. Interrupting the parent
delegates the granularity question to the only process that knows the tree, and covers
the no-child case with the same signal.

**Send `C-c` to the pane** (`SessionBackend.send_keys`, what `_graceful_kill_session` uses).
The pty's INTR character goes to the pane's whole foreground process group, which
contains the `tee` — killing the pipe the recovery narrates into, and with it the log
whose mtime is this reclaim's own progress evidence. Signalling the pid keeps the
narration flowing, which is what lets the next round see that the interrupt worked.

**Tighten `_pause_owner()` so a stalled rollout stops counting as an owner.** It runs
during a pause, which is the hard requirement, and it needs no new controller. But it is
the wrong host: `_pause_owner` runs on every machine, and only the gateway has the
rollout session and its log. On an agent-runner the log does not exist, so "no progress"
would be true of every host in every healthy rollout, and the fleet would unpause itself
mid-deploy. Its deference to the lease is exactly what must not change.

**A second, longer timeout for the rollout.** Rejected for the reason
`shared/deploy_timing.py` was written: two clocks that disagree about "stopped making
progress" are two chances to declare a working rollout dead, or a dead one working. The
gap this closes is a missing *consumer* of that number, not a missing number.

## Consequences

- An `ava update` that hangs converts to a failure plus in-process recovery in ~15-30
  minutes with nobody watching, instead of standing until an operator notices.
- **An operator's Ctrl-C on a foreground `ava update --local` now also rolls the gateway
  back to last-known-good** instead of exiting on a traceback with the cluster mid-
  transition. That is a behaviour change beyond the unattended case, and the intended
  one: an abort that leaves a working cluster is what Ctrl-C should always have meant.
  A restart-only bounce has no snapshot, so its interrupt still propagates.
- The unattended power added is "send one `SIGINT` to a process that has written nothing
  for 15 minutes". Everything after that is the rollout's own abort path.
- **A silent step longer than 900 s is now a liability anywhere in the orchestration.**
  Phase B was the one that existed and it now writes a heartbeat; any future step that
  can block that long without output has to do the same, or it will be interrupted while
  working. This is a deliberate constraint, not an oversight — the alternative is a
  bound nothing can be checked against.
- The stage-2 force kill is strictly worse than an operator's `ava cluster recover` and
  says so in its own log line. It exists because a session that outlives its process
  refuses every subsequent `ava update` cluster-wide via `current_orchestration()`.
- `LastUpdate` now carries `holder`. The column was always selected and used only inside
  the `RUNNING`/`ORPHANED` derivation; exposing it is what lets the reclaim name the
  process without a second read of the lease.
