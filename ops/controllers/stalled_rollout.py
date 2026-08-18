"""Stalled-rollout controller — the no-progress clock, pointed at the orchestration
that drives the whole cluster.

`NO_PROGRESS_TIMEOUT_S` already governs three questions (`shared.deploy_timing`) and
`ops.controllers.stalled_updater` already turns it into an unattended reclaim — but
only for `ava-updater`, a host's self-update. The `ava-rollout` session, which pauses
every agent-runner and stops this host's services before it does anything else, was
outside every clock in the system. On 2026-08-02 a rollout hung inside `converge` on a
`codesign` child waiting for a GUI authorization that no detached session can ever
answer. It held that state for 67 minutes with the cluster stopped, and **every
self-healing layer stood down correctly**: `stranded_pause` saw a live lease, the
resurrected watchdog saw a paused host, `ava cluster recover` saw a live holder pid.
Each of them asks whether the owner is *alive*. None asks whether it is *getting
anywhere*, and a process blocked forever on a prompt is maximally alive.

## The evidence: two marks, and why neither alone works

A rollout is stalled when nothing it writes has moved in `NO_PROGRESS_TIMEOUT_S`. The
mark is `max(started_at, rollout-log mtime)`:

- the **log mtime** is the real progress signal — the orchestration narrates every
  phase through the `tee` `spawn_rollout` put on its pipeline;
- **`started_at`** is what stops a *young* rollout from reading as an ancient one. The
  log file is created by that `tee`, not by `spawn_rollout`, so between the
  session spawning and the shell's first write there is a moment when the
  newest `rollout-*.log` on disk belongs to the PREVIOUS rollout and is hours old.
  Globbing for the newest log — which is all `_reap_stalled_updater` can do, since a
  self-update has no cluster-wide record — would read that as a stalled brand-new
  session and interrupt a healthy rollout in its first second.

Both come from `cluster_last_update`, and the row is used only when it reads
`RUNNING`: an open row whose recorded holder still holds the deploy lease. That is
this repo's existing definition of "an orchestration is executing right now"
(`shared.last_update`), and it is stricter than the session probe on exactly the axis
that matters — a stale row from a finished rollout resolves to a terminal outcome, and
one from a dead rollout resolves to `ORPHANED`, so neither can be mistaken for the run
whose session we are looking at. Every unreadable or ambiguous signal returns "not
stalled": misjudging a working rollout costs the cluster a stop-the-world abort, while
missing one costs another round.

## The action: interrupt the orchestrator, do not kill the session

The recovery for a failed rollout is **inside the rollout process** — the `finally` in
`cli.commands.update`, which unpauses this host, dials every paused agent-runner back
to resumed, writes the terminal `cluster_last_update` row and prints the aftermath. A
a force-kill of the session destroys the only process that can run it and leaves the cluster
exactly where the hang left it: stopped and paused, waiting on the deploy lease to
lapse. So the first move is not a kill at all.

**Stage 1 — `SIGINT` to the orchestration's own pid.** The lease holder string is
`<machine>:pid<N>` (`shared.cluster_lock.self_holder`), so the record names the process
directly; it is signalled only when the holder names THIS machine and that pid is
alive, because a pid read out of another host's holder string is a local pid belonging
to somebody else. Python turns `SIGINT` into `KeyboardInterrupt`, which is a
`BaseException` — no `except Exception:` in the orchestration swallows it — so the
`finally` runs and the cluster recovers itself along the path that was already tested.

That also settles the granularity question the other way round: **nothing here picks a
leaf to kill.** The unwinding does it, and does it better than an outside guess could:
`shared.proc.run_bounded` catches `BaseException` specifically to `kill_process_tree`
before re-raising, so the interrupt takes out the hung tool *and every descendant it
spawned*, while a plain `subprocess.run` still kills its own child. A hang with no
child at all — a deadlock in the orchestrator itself — is covered by the same signal.

Signalling the pid rather than sending `C-c` to the pane matters for a second reason:
the pane's foreground process group contains the `tee`, and killing that would close
the pipe the recovery narrates into — the log we are measuring.

**Stage 2 — force-kill the session, only after stage 1 has provably failed.** A process
group that ignored an interrupt for a further full no-progress window is not going to
run its `finally`, and the session must not hold `current_orchestration()` forever —
that answer alone refuses the next `ava update` cluster-wide and blocks this host's
pin, code and pause controllers (`ops.manager`). After a force-kill the recovery falls
to the layer that already exists for a hard-killed rollout: the lease stops being
renewed, lapses at `LOCK_TTL_S`, and `stranded_pause` recovers a pause that has
outlived its owner. That is slower and cruder than stage 1, which is why it is second.

The stage is chosen by the persistent heal record (`_heal_record`, keyed on the run's
holder + start), not by a second clock: one interrupt per rollout. Re-sending it every
round would land the second one *inside* the recovery the first one started, aborting
the compensating resume half way through the fleet.

## Where it runs, and why that is not the obvious place

Gateway capability, ahead of `PauseController` — the position
`ops.controllers.stalled_updater` established and for a sharper version of its reason.
A rollout pauses this host as part of Phase A, so this controller only ever meets its
target on a paused host, and `PauseController` blocks the round with `BlockScope.ALL`.
Anything registered behind it is short-circuited away in precisely the case it exists
for; the 2026-08-02 log says so in the only line the box emitted for 67 minutes,
`round blocked by pause (scope=all)`. Ahead of it is safe for the same reason as the
updater reaper: this controller never blocks, because it removes a signal rather than
touching a service.

Gateway rather than agent-runner is where the evidence is: the `ava-rollout` session
and its log are on the host that spawned them. An agent-runner has neither and must
keep deferring to the lease, which is what makes a mid-rollout fleet hold still.

The watchdog process itself is stopped by the rollout's own local leg (`ava stop`
precedes the pull, and `converge` — where the 2026-08-02 rollout hung — runs before
`ava start` brings anything back up). It is the OS-scheduled `ava cluster
watchdog-probe` that revives it (`shared.os_watchdog_probe`), which is why this
controller gets to run at all during a hang that has taken the host's services down.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from ops.controllers import _heal_record
from ops.controllers.base import BlockScope, ReconcileResult
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.last_update import LastUpdate, UpdateOutcome, read_last_update
from shared.machine import MachineRole

_log = logging.getLogger("ops.controllers.stalled_rollout")

# The family's one definition of "stopped making progress" (`shared.deploy_timing`),
# shared with the hung-updater reaper, the settle-hold TTL and the Phase-B poll. A
# rollout that is silent for this long is hung, and — since `cli.commands.update`'s
# Phase-B poll now writes a heartbeat on the lease-renewal cadence — no phase of a
# healthy rollout is silent for anything close to it.
_ROLLOUT_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S


def _reclaim_attempt_path() -> Path:
    from shared.paths import ava_home

    return ava_home() / "rollout_reclaim_attempt"


@dataclass(frozen=True)
class _StalledRollout:
    """A rollout this host is running that has stopped making progress.

    Built only from a `RUNNING` `cluster_last_update` row, so it describes the run
    whose session is alive right now and never a previous one. `episode` is that run's
    identity for the heal record — the holder (`<machine>:pid<N>`) plus its start
    second, so neither a recycled pid nor a re-read of the same row can make one run's
    interrupt count as another's.
    """

    holder: str
    episode: str
    idle_s: float
    log: str | None


def _running_rollout() -> tuple[str, float, str | None] | None:
    """`(holder, started_at_epoch, log_path)` iff `cluster_last_update` reads
    `RUNNING`, else None.

    Never raises: a controller round must survive an unreachable data plane, and the
    reading it degrades to ("no rollout of ours is executing") is the one that acts on
    nothing.
    """
    try:
        record: LastUpdate | None = read_last_update()
    except Exception:
        _log.warning(
            "[ops.rollout] could not read the last-update record; deferring the stall check"
        )
        return None
    if record is None or record.outcome is not UpdateOutcome.RUNNING:
        return None
    if record.started_at is None or record.holder is None:
        # `RUNNING` is derived from `started_at` being set and the row's holder matching
        # the live lease, so neither can be None here. Refuse anyway rather than assume:
        # the holder is about to be turned into a pid and signalled.
        return None
    return record.holder, record.started_at.timestamp(), record.log_path


def _progress_age(started_at: float, log_path: str | None) -> float:
    """Seconds since this rollout last made visible progress.

    `started_at` is a floor under the log's mtime, not an alternative to it — see the
    module docstring for the window in which the newest `rollout-*.log` on disk is
    still the *previous* rollout's. Postgres and this process are on the same box (the
    gateway owns the data plane), so comparing `now()` against `time.time()` is not a
    cross-clock comparison.
    """
    last_progress = started_at
    if log_path:
        try:
            last_progress = max(last_progress, Path(log_path).stat().st_mtime)
        except OSError:
            # The log named by the record is unreadable. `started_at` still bounds the
            # run from below, and a rollout whose own log has vanished is not one to
            # give extra benefit of the doubt to.
            _log.warning("[ops.rollout] rollout log %s is unreadable; using started_at", log_path)
    return time.time() - last_progress


def _stalled_rollout() -> _StalledRollout | None:
    """This host's rollout, iff its session is alive and it has stopped making
    progress. None on every other reading, including every unreadable one."""
    import shared.cluster
    from ops.cluster_session import _ROLLOUT_SERVICE, _has_orchestration_session

    try:
        session = shared.cluster.session_name(_ROLLOUT_SERVICE)
        alive = _has_orchestration_session(session)
    except Exception:
        _log.warning("[ops.rollout] could not probe the rollout session; deferring")
        return None
    if not alive:
        # No rollout here. Drop the record so the next rollout starts at stage 1 — a
        # backoff that outlived its own run would force-kill the following one without
        # ever interrupting it.
        _heal_record.clear(_reclaim_attempt_path())
        return None
    running = _running_rollout()
    if running is None:
        return None
    holder, started_at, log = running
    idle_s = _progress_age(started_at, log)
    if idle_s < _ROLLOUT_STALL_TIMEOUT_S:
        return None
    return _StalledRollout(
        holder=holder, episode=f"{holder}@{int(started_at)}", idle_s=idle_s, log=log
    )


def _local_holder_pid(holder: str) -> int | None:
    """The live local pid `holder` names, or None.

    Same parse as `ops.ops_cluster._lock_holder_is_live` and the same refusals, with
    the verdict inverted: that one asks "may I clobber this lock" and answers "no" on
    every doubt, this one asks "may I signal this pid" and answers None on the same
    ones. A holder naming another machine is the load-bearing refusal — its pid is
    meaningless in this host's namespace, and signalling it would hit an unrelated
    local process.
    """
    from shared.machine import machine_name
    from shared.proc import process_alive

    machine, sep, pid_str = holder.partition(":pid")
    if sep == "":
        return None
    try:
        if machine != machine_name():
            return None
    except Exception:
        _log.warning("[ops.rollout] cannot resolve this machine's name; not signalling %s", holder)
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    return pid if process_alive(pid) else None


def _interrupt(stalled: _StalledRollout) -> bool:
    """Stage 1: `SIGINT` the orchestration so it runs its own recovery. True if sent."""
    pid = _local_holder_pid(stalled.holder)
    if pid is None:
        _log.error(
            "[ops.rollout] rollout stalled %.0fs but its holder %r does not name a live process "
            "on this host, so there is nothing to interrupt; the session will be force-killed "
            "once the interrupt window lapses",
            stalled.idle_s,
            stalled.holder,
        )
        return False
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        _log.error("[ops.rollout] could not interrupt rollout pid %d: %r", pid, exc)
        return False
    _log.warning(
        "[ops.rollout] rollout %s has written nothing to %s in %.0fs — interrupting pid %d so it "
        "runs its own abort (unpause this host, resume every paused agent-runner, record the "
        "outcome). Force-killing the session if that changes nothing in another %.0fs.",
        stalled.holder,
        stalled.log or "its log",
        stalled.idle_s,
        pid,
        _ROLLOUT_STALL_TIMEOUT_S,
    )
    return True


def _force_kill(stalled: _StalledRollout) -> bool:
    """Stage 2: kill the session. True if it is confirmed gone.

    Nothing here drives the recovery afterwards, and that is the honest shape rather
    than a gap: the process that carried it is being destroyed. What follows is the
    path a hard-killed rollout has always taken — the lease stops being renewed and
    lapses at `LOCK_TTL_S`, after which `stranded_pause` finds an unowned pause and
    unpauses this host, and the agent-runners do the same for theirs.
    """
    # The rollout session lives on the service session backend (S7 — the same
    # backend as every other session; before S7 it stayed on its own), so the kill
    # goes to get_backend(), never get_shell_backend().
    import shared.cluster
    from ops.cluster_session import _ROLLOUT_SERVICE
    from shared.session_backend import get_backend

    session = shared.cluster.session_name(_ROLLOUT_SERVICE)
    try:
        killed, _mode = get_backend().kill_session(session, graceful=False)
    except Exception:
        _log.exception("[ops.rollout] failed to kill stalled rollout session %s", session)
        return False
    if not killed:
        _log.error(
            "[ops.rollout] backend declined to kill stalled rollout session %s; it will "
            "keep refusing every `ava update` cluster-wide. Kill it by hand: terminate the "
            "pid named in $AVA_HOME/run/sessions/%s.json",
            session,
        )
        return False
    _log.error(
        "[ops.rollout] rollout %s ignored an interrupt for %.0fs — force-killed session %s. Its "
        "own abort did NOT run: this host stays paused with its services down until the deploy "
        "lease lapses and the stranded-pause recovery lifts it. `ava cluster recover` is the "
        "faster path if you are watching.",
        stalled.holder,
        stalled.idle_s,
        session,
    )
    return True


def reclaim_stalled_rollout_if_hung() -> bool:
    """Interrupt — then, if that fails, kill — a rollout that has stopped making
    progress. True if this call acted.

    Runs once a gateway watchdog round. Never raises: the manager wraps no controller
    in a try/except, so an exception here would take the watchdog down.
    """
    try:
        stalled = _stalled_rollout()
        if stalled is None:
            return False
        path = _reclaim_attempt_path()
        prior = _heal_record.read_record(path)
        if prior is None or prior.get("target") != stalled.episode:
            # Nothing on record for THIS run: stage 1. The question is deliberately
            # equality on the episode rather than `in_backoff`'s time window — "have I
            # already interrupted this rollout" must stay true for as long as the run
            # exists, or a rollout that outlives the window gets a second interrupt
            # straight into the recovery the first one started.
            sent = _interrupt(stalled)
            _heal_record.record_attempt(path, stalled.episode, ok=sent)
            return sent
        if _heal_record.in_backoff(path, stalled.episode, _ROLLOUT_STALL_TIMEOUT_S):
            _log.info(
                "[ops.rollout] rollout %s was interrupted and has not written since (%.0fs); "
                "leaving its abort room to run",
                stalled.holder,
                stalled.idle_s,
            )
            return False
        acted = _force_kill(stalled)
        _heal_record.record_attempt(path, stalled.episode, ok=acted)
        return acted
    except Exception:
        _log.exception("[ops.rollout] stalled-rollout reclaim failed; retrying next round")
        return False


class StalledRolloutController:
    """Runs the stalled-rollout reclaim as one manager controller.

    Gateway only — the `ava-rollout` session and the log that is its progress signal
    live on the host that spawned them, and an agent-runner has neither. On a single
    box the two capability watchdogs both run, and only the gateway one takes this leg;
    on a split deployment the runners keep deferring to the deploy lease, which is what
    holds the fleet still through a healthy rollout.
    """

    name = "rollout"

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        if role != "gateway":
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        acted = reclaim_stalled_rollout_if_hung()
        # Never blocks. Interrupting a rollout hands the host to the controllers that
        # own the dimensions it left wrong — stranded-pause lifts the pause, code
        # restarts stale processes — and none of them can act while this round is
        # short-circuited.
        return ReconcileResult(
            dimension=self.name,
            blocks=BlockScope.NONE,
            acted=acted,
            detail="reclaimed a rollout that stopped making progress" if acted else None,
        )
