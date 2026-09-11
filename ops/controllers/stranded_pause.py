"""Pause controller — the cluster-pause dimension.

Desired state (from Spec): a host is paused only while a rollout owns it. When the
posture row reads ``paused``/``converging`` (R1, Task #1021), the tick's
healthchecks must be skipped — otherwise this tick's restarter healthcheck would
immediately revive the restarter that Phase A killed and fight the rollout. The
posture returns to ``idle`` at the end of ``ava start``.

**Stranded-pause recovery** (Approach B): a gateway rollout pauses agent-runners in
Phase A; if the orchestration is then hard-killed (SIGKILL / rollout session
killed / machine crash), the finally-resume and the recovery layer never run, so the
host stays paused forever. So a pause that has outlived its owner is recovered here.

**Who owns a pause is a two-signal question** (``_pause_owner``). The cluster update
lease covers a gateway orchestration — the only actor that pauses a host it is not
running on. A live local updater lease (`host_deploy_state.updater_lease_expires_at`,
claimed first thing by the updater chain, R1 PR5) covers the updater this host
spawned for itself (the schema controller's heal, the pin controller's local
fallback, the code controller's restart), which takes no cluster lease at all and
which ``spawn_update`` pauses this host for *before* that session exists.

Asking both is what makes the wait short. On the lease alone, "nobody holds the lease"
and "nothing is executing" are indistinguishable, so a single conservative bound had
to cover the case where a local self-update was quietly working — and every pause
whose owner was already dead paid that same ten minutes, with ``ops.manager`` blocking
its whole roster for the duration. That is the state a failed updater leaves behind:
its ``ava start`` recovery exits before the step that returns the posture to idle when
the schema check refuses, so the paused posture outlives its owner (issue #1074).

One bound follows, not two. An **owned** pause is declined outright, exactly as before
— a live lease or a live session means someone is coming back, and the existing
mechanisms end those states on their own (the lease's TTL stops reporting a crashed
holder; the stalled-updater reaper, which runs ahead of this controller, kills a
session that stopped writing). An **unowned** pause waits only
``STRANDED_PAUSE_TIMEOUT_S``, which drops from 600s to 120s because that number no
longer has to cover the blind spot — it only has to outlast the spawn gap above.

**A settle hold is not an owner.** ``update_lock_holder()`` is a lossy read: it
collapses "an orchestration is executing" and "a stated waiting period, nobody
executing" into one truthy holder (see ``shared.cluster_lock``). The second of those
is a **settle hold**, and a settle hold naming THIS host is the gateway's own record
that this host's pause has lost its owner — the ``POLL_STALLED`` verdict behind it is
minted (``cli.commands.update._probe_verdict``) from ``paused=true`` **and**
``current_orchestration=null``, which is this module's unowned reading taken remotely
and confirmed twice. Reading that record back as proof of ownership closes a
contradiction loop: the more conclusively the orchestration established that nobody
is coming back for this pause, the longer the pause was kept. So the lease half asks
``DeployLease.awaits(machine_name())`` — the same discrimination ``ops.controllers``'
``code`` and ``pin`` make (issue #1020) — and a hold naming this host is not an owner.
It does not shortcut the wait: such a pause becomes UNOWNED and serves
``STRANDED_PAUSE_TIMEOUT_S`` like any other, because the spawn gap that bound covers
is still there.

**A live lease whose gateway has been unreachable is dead evidence** (issue #2101).
A cluster update runs on the gateway host, and every leg of it — Phase A fan-out,
the gateway's own restart, Phase B — needs that host's gateway process. When the
gateway the lease holder manages has answered no probe for
``GATEWAY_DOWN_OWNER_GRACE_S`` (healthz unreachable, not merely degraded), the
orchestration cannot be executing anything: it is dead, or stuck where the
controllers ahead of this one reap it. The lease then does not own the pause and
the local-session check still decides — consulted, and still authoritative. The
evidence is a host-local down-since marker maintained by the gateway-capability
watchdog (``record_gateway_reachability``); it survives watchdog restarts, and a
pure agent-runner never accumulates it (a runner cannot tell a gateway outage from
a partition, so it keeps the conservative reading).

**Why it had to be fixed here rather than downstream.** ``PauseController`` blocks
with ``BlockScope.ALL``, so ``ops.manager`` short-circuits and the pin and code
controllers never run on a paused host — the two that carry that same settle-hold
exception, and the two that produce the convergence
``ops.deploy_window.settle_hosts_converged`` re-probes for. The one host the hold was
waiting for was the one host forbidden to converge, so the hold could never release
early and lapsed on ``SETTLE_TTL_S`` (900s) every time, with ``ops.manager``'s ERROR
escalation firing at 10 rounds along the way.

Extracted from ``services.watchdog.daemon`` (``_recover_stranded_pause`` /
``_stranded_pause_seconds`` + the ``_tick`` pause gate). Host-level — both
capability watchdogs run it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path

from ops import cluster_session
from ops.cluster import unpause_local_cluster
from ops.controllers.base import BlockScope, ReconcileResult
from shared import pause_owner, ui_update_state, updater_handoff
from shared.cluster_lock import read_update_lease
from shared.deploy_timing import GATEWAY_DOWN_OWNER_GRACE_S
from shared.machine import MachineRole, machine_name
from shared.platform import LockTimeoutError

_log = logging.getLogger("ops.controllers.stranded_pause")

# How long an UNOWNED pause waits before recovering — and unowned is the only kind
# this timer ever sees, because an owned one is declined outright below.
#
# It was 600s, and it had to be: `_pause_owner` could only read the lease, so a pause
# held by a working local self-update (which takes no lease) was indistinguishable
# from one whose owner was dead, and the bound had to cover the longest legitimate
# rollout. Now that the local session is a signal too, "nothing is executing" is
# provable, and the only thing this wait must survive is the gap inside
# `ops.cluster.spawn_update` between `pause_local_cluster()` and the
# new-session` a few statements later — sub-second in practice, against a 60s
# watchdog round. Every second beyond that is a second `ops.manager` blocks this
# host's whole roster on a pause nobody is coming back for.
STRANDED_PAUSE_TIMEOUT_S = 120.0  # 2 min

# How long an ownerless maintenance hold must persist before the controller
# DECLARES it stranded (task #3132) — the failure state a dead updater leg
# leaves. Recovery deliberately does not touch a maintenance hold (an
# incomplete stop is never resumed by a controller), so before this record the
# state was silent, permanent and roster-invisible; the declaration is what the
# gateway-side alarm and the roster read (host_deploy_state.stranded_hold_*).
#
# The bound reuses `ops.manager`'s standing judgment — ten rounds, no
# legitimate transition holds this host longer (_BLOCKED_ROUND_ALARM_ROUNDS,
# ten minutes at the 60s round) — rather than inventing a second one. It only
# ever needs to outlast the ownerless gaps a healthy transition has (the
# spawn gap inside `spawn_update`, observed tens of seconds worst case) plus
# one watchdog round of clock slack; anything below the manager's bound would
# alarm on states the manager still treats as ordinary.
STRANDED_HOLD_NOTICE_S = 600.0  # 10 min

# The reachability-evidence grace (value, ordering and rationale live in the
# clock lattice — see the module-level import of `GATEWAY_DOWN_OWNER_GRACE_S`).
_GATEWAY_DOWN_MARKER = "gateway-down-since"


def _gateway_down_marker_path() -> Path:
    import shared.paths

    return shared.paths.run_dir() / _GATEWAY_DOWN_MARKER


def _probe_gateway_reachable() -> bool:
    """Whether the gateway answers its health URL at all.

    Any HTTP response (even a 503 — the process is alive but degraded) counts as
    reachable: the question is "can the gateway-side orchestration still be
    executing", and a process that answers can be driven. Only connection errors
    and timeouts (the event-loop freeze shape) read as unreachable."""
    import httpx

    from shared.config import settings

    try:
        httpx.get(settings.services.gateway_health_url, timeout=2.0)
    except httpx.HTTPError:
        return False
    return True


def record_gateway_reachability() -> None:
    """Maintain the host-local gateway-down-since marker.

    Called by the pause controller each round on the gateway-capability watchdog.
    Reachable clears the marker; unreachable stamps it once (the FIRST down round
    is the evidence's anchor — a later probe keeps the original timestamp so the
    grace bound measures the continuous outage, not the last failed probe).

    Best-effort on both sides: an unwritable run dir must not break the tick, and
    a missing marker reads as no evidence (the conservative, lease-owns path)."""
    path = _gateway_down_marker_path()
    if _probe_gateway_reachable():
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        tmp = path.with_name(f".{_GATEWAY_DOWN_MARKER}.tmp")
        tmp.write_text(str(time.time()))
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam


def _gateway_down_seconds() -> float | None:
    """How long the gateway has been unreachable, or None when there is no
    evidence (marker absent, unreadable, or a backwards clock — all read as
    no-evidence so the lease keeps owning the pause)."""
    path = _gateway_down_marker_path()
    try:
        ts = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    elapsed = time.time() - ts
    return elapsed if elapsed >= 0 else None


def is_paused() -> bool:
    """Whether this host is paused or converging (R1, Task #1021 — the
    `host_deploy_state.posture` row).

    ``converging`` gates the roster exactly like ``paused``: the updater is
    running with the restarter deliberately down, and reviving services under
    it is the fight the pause exists to prevent. (The gateway's 503 middleware
    reads only ``paused`` — the data plane is a gateway-host concern and a
    gateway host never runs an updater session.)
    """
    from shared import maintenance
    from shared.host_deploy_state import read

    if maintenance.held():
        return True

    try:
        state = read()
    except Exception:
        _log.warning(
            "[ops.pause] host_deploy_state read failed; reading as not paused",
            exc_info=True,
        )
        return False
    return state is not None and state.posture in ("paused", "converging")


def _stranded_pause_seconds() -> float | None:
    """How long this host has been paused/converging, or None when it is not
    paused or the row cannot be read.

    The anchor is `paused_at` (set when the posture enters `paused`, preserved
    through `converging`), NOT `updated_at`: every transition inside the pause
    window bumps `updated_at`, including each updater-lease renewal — which is
    what kept a stranded 2026-09-10 pause reading "fresh" for 122 rounds while
    its owner was dead, so the owner check never ran (issue #2101). Rows from
    before the `paused_at` column existed fall back to `updated_at`."""
    from shared.host_deploy_state import read

    try:
        state = read()
    except Exception:
        _log.warning("[ops.pause] cannot read host_deploy_state; skipping stranded-pause check")
        return None
    if state is None or state.posture not in ("paused", "converging"):
        return None
    anchor = state.paused_at if state.paused_at is not None else state.updated_at
    return time.time() - anchor.timestamp()


def _pause_owner(
    handoff: updater_handoff.UpdaterHandoffSnapshot | None = None,
) -> str | None:
    """Who, if anyone, is still executing the transition this pause belongs to.

    Two signals, because neither covers the other. The **cluster update lease** covers
    a gateway orchestration, which is the only actor that can pause a host it is not
    running on. A **local updater lease** covers the updater this host spawned for
    itself — the schema controller's heal, the pin controller's local fallback, the
    code controller's restart — which takes no cluster lease at all, and which
    `ops.cluster.spawn_update` pauses this host for before it even exists. The
    updater claims its lease as the first thing the chain does (R1, PR5), so the
    claim gap is sub-second.

    The lease is asked the *precise* question, not "is it held": a *settle hold naming
    this host* is a stated waiting period with nothing executing under it, and it is
    the orchestration's own record that this pause lost its owner (module docstring).
    It is therefore not an owner, and the pause falls through to the local-session
    check — which still runs, unchanged. That scoping is the whole of it: a lease with
    no note (a rollout executing right now) and a hold naming somebody else both still
    own this pause, and a settle hold never licenses an unpause by itself, only a
    reading of the *other* signal.

    Returns None only when both say no, which is the one reading that licenses an
    unpause. A signal that cannot be read returns a placeholder owner rather than None:
    an unpause taken on missing evidence is the one mistake this function must not
    make — which is why `machine_name()` is inside the guarded read too. Not knowing
    which host this is means not knowing whether a hold names it.
    """
    from shared import maintenance

    if maintenance.held():
        return "explicit maintenance hold (no automatic expiry)"
    return _executing_owner(handoff)


def _executing_owner(
    handoff: updater_handoff.UpdaterHandoffSnapshot | None = None,
) -> str | None:
    """Who is still executing a transition here, ignoring the maintenance hold.

    The body of the two-signal owner determination minus the hold short-circuit:
    `_pause_owner` answers "may this pause be resumed", and any maintenance hold
    is an owner of that question (nobody resumes a held unit but the explicit
    `ava start`). The stranded-hold verdict (task #3132) asks the narrower
    question behind it — is anything executing DESPITE the hold — because a hold
    with no live handoff, session, deploy lease or orchestration is exactly the
    failure state a dead updater leg leaves, and it is the one state nothing
    else on the host can see. Same placeholder-on-unreadable discipline as
    `_pause_owner`: only `None` is proof that nothing is executing.
    """
    handoff = updater_handoff.read() if handoff is None else handoff
    if handoff.status == "invalid":
        return "updater handoff is unreadable"
    if handoff.status == "pending" and not handoff.expired:
        return f"updater handoff {handoff.generation} is pending"
    if handoff.status == "running" and updater_handoff.owner_is_live(handoff):
        return f"updater handoff {handoff.generation} has a live process owner"
    try:
        lease = read_update_lease()
    except Exception:
        _log.warning("[ops.pause] could not read update lock; deferring stranded-pause recovery")
        return "unreadable update lock"
    try:
        awaited = lease is not None and lease.awaits(machine_name())
    except Exception:
        # Distinct from the read above on purpose: "the DB is unreachable" and "this
        # host does not know its own name" send an operator to different places.
        _log.warning(
            "[ops.pause] could not resolve this machine's name, so whether the deploy lease "
            "names this host is unanswerable; deferring stranded-pause recovery"
        )
        return "unresolvable machine name"
    if lease is not None and awaited:
        _log.warning(
            "[ops.pause] the deploy lease is a settle hold waiting for THIS host (%s) — nothing "
            "executes under it and it is the record that this pause lost its owner, so it does "
            "not own the pause; the local-session check still decides",
            lease.describe(),
        )
    elif lease is not None:
        down_s = _gateway_down_seconds()
        if down_s is not None and down_s > GATEWAY_DOWN_OWNER_GRACE_S:
            _log.warning(
                "[ops.pause] the gateway has been unreachable for %.0fs (grace %.0fs) — the "
                "lease holder's orchestration cannot be executing this pause, so the lease is "
                "dead evidence; the local-session check still decides",
                down_s,
                GATEWAY_DOWN_OWNER_GRACE_S,
            )
        else:
            return f"a cluster update holds the lock ({lease.holder})"
    try:
        live_session = cluster_session.live_orchestration_session()
    except Exception:
        _log.warning(
            "[ops.pause] could not probe local orchestration sessions; deferring "
            "stranded-pause recovery"
        )
        return "unreadable orchestration session"
    if live_session is not None:
        return f"local orchestration session {live_session} is in flight"
    from ops.cluster import current_orchestration

    try:
        orchestration = current_orchestration()
    except Exception:
        _log.warning(
            "[ops.pause] could not read the orchestration session; deferring stranded-pause "
            "recovery"
        )
        return "unreadable orchestration session"
    return f"a local {orchestration} is in flight" if orchestration is not None else None


def stranded_hold_verdict(
    handoff: updater_handoff.UpdaterHandoffSnapshot | None = None,
) -> tuple[str, float] | None:
    """`(reason, paused_for_s)` when this host is a STRANDED maintenance hold, else None.

    The failure state of task #3132: a maintenance hold — the deliberately
    non-expiring pause a stop arms, releasable only by an explicit authorized
    `ava start` — whose owning process is gone. Three facts together:
    nothing executes under it (`_executing_owner` reads no live handoff,
    session, deploy lease or orchestration), this host's own updater record
    says the run that armed it FAILED (`exited` non-zero, or `unknown` — died
    mid-flight; a `declined` run stopped nothing and a successful one would
    have released the hold), and the state has outlived
    `STRANDED_HOLD_NOTICE_S` so no in-flight transition can be misread as one.

    Recovery deliberately does NOT auto-release such a hold, and this verdict
    does not change that: it exists to make the state loud and visible
    (`sync_stranded_hold_record`, the gateway-side alarm, the roster) instead
    of silent and permanent. An operator's own `ava maintenance stop` produces
    no failed updater outcome, so it never matches — deliberately held units
    stay quiet.
    """
    from shared import maintenance

    paused_for = _stranded_pause_seconds()
    if paused_for is None or paused_for < STRANDED_HOLD_NOTICE_S:
        return None
    if not maintenance.held():
        return None
    if _executing_owner(handoff) is not None:
        return None
    from ops.updater_outcome import last_updater_outcome

    outcome = last_updater_outcome()
    if outcome is None or outcome.kind == "declined":
        return None
    if outcome.kind == "exited" and (outcome.rc or 0) == 0:
        return None
    reason = (
        f"updater exited rc={outcome.rc}" if outcome.kind == "exited" else "updater died mid-flight"
    )
    return reason, paused_for


def sync_stranded_hold_record(
    handoff: updater_handoff.UpdaterHandoffSnapshot | None = None,
) -> None:
    """Bring this host's durable stranded-hold record in line with the verdict.

    Declares while `stranded_hold_verdict` holds — set-once, and logged at
    ERROR on the transition so the log carries the incident boundary and the
    recourse — and clears otherwise (including on every unpaused round), so
    the record cannot outlive the condition that justified it. Never raises:
    it is a side band beside the recovery decision, and a DB blip must cost a
    round, not the tick.
    """
    from shared.host_deploy_state import clear_stranded_hold, mark_stranded_hold

    try:
        verdict = stranded_hold_verdict(handoff)
        if verdict is None:
            clear_stranded_hold()
            return
        reason, paused_for = verdict
        if mark_stranded_hold(reason):
            _log.error(
                "[ops.pause] STRANDED HOLD declared: this host has been held for %.0fs "
                "(%s) with nothing executing under the pause — the state a failed "
                "update leg leaves. Not unpausing (an incomplete stop is never resumed "
                "automatically); nothing else will resume it either — run `ava start` "
                "on this host.",
                paused_for,
                reason,
            )
    except Exception:
        _log.warning(
            "[ops.pause] stranded-hold record sync failed; retrying next round",
            exc_info=True,
        )


def pause_owner_verdict(
    handoff: updater_handoff.UpdaterHandoffSnapshot | None = None,
) -> str | None:
    """Public spelling of the pause owner determination.

    The gateway healthcheck's respawn gate (issue #2101) asks the same question
    this controller does, so the two share one reading: None = unowned, any
    string = the owner that is still executing the transition.
    """
    return _pause_owner(handoff)


def recover_stranded_pause() -> bool:
    """Self-unpause a host whose pause has no owner left. Returns True if it did.

    "Nobody has the lease" and "nothing is executing" are not the same claim and used
    to be conflated. A watchdog-spawned updater takes no lease
    (`ops.controllers.stalled_updater` says so, and it is why the reaper exists), so
    the lease-only test could not see one at all — which is what forced the wait to be
    long enough to cover a self-update that might be quietly working.

    - **Owned** — a live *executing* lease, or a live orchestration session. Declined
      outright, unchanged from before: someone is coming back. A crashed holder stops
      being a live lease at its TTL and a hung session is killed by the reaper that
      runs ahead of this controller, so both resolve into the unowned case rather than
      needing a second timer here. A **settle hold naming this host** is the one lease
      shape that is not ownership — nobody is executing under it and nobody is coming
      back, which is precisely what the orchestration wrote it down to say.
    - **Unowned** — neither. Nothing is coming back to resume this host, and the only
      thing an unpause must not race is the gap between `spawn_update`'s
      `pause_local_cluster()` and the session spawn a few statements later. This
      is the state a failed updater leaves behind: its `ava start` recovery exits
      before the step that unlinks the flag when the schema check refuses, so the flag
      outlives its owner and `ops.manager` blocks every round on `pause` — which is how
      "the gateway is on the wrong commit" became "nothing gets revived" for two hours
      (issue #1074).

    Lock contention on the lifecycle mutex (another process writing maintenance
    state) is expected, not fatal: recovery defers with False and the pause is
    kept. Only a real I/O or ownership error raises.

    Safe to call standalone: with no paused posture / a fresh pause it returns False
    without acting."""
    paused_for = _stranded_pause_seconds()
    if paused_for is None or paused_for <= STRANDED_PAUSE_TIMEOUT_S:
        return False
    try:
        with ui_update_state.lifecycle_lock():
            # The first age check avoids taking the cross-process mutex on ordinary
            # ticks. Re-read under it so a fresh owner cannot appear between proof
            # and the destructive unpause/marker clear.
            paused_for = _stranded_pause_seconds()
            if paused_for is None or paused_for <= STRANDED_PAUSE_TIMEOUT_S:
                return False
            handoff = updater_handoff.read()
            owner = _pause_owner(handoff)
            if owner is not None:
                _log.info(
                    "[ops.pause] paused for %.0fs but %s — that transition still owns this "
                    "pause, not unpausing",
                    paused_for,
                    owner,
                )
                return False
            if not updater_handoff.allows_generic_recovery(handoff):
                _log.warning(
                    "[ops.pause] retained updater recovery requires an explicit checked "
                    "recovery; refusing generic self-unpause"
                )
                return False
            snapshot = ui_update_state.read()
            pause_snapshot = pause_owner.read()
            _log.warning(
                "[ops.pause] paused for %.0fs and no update is executing (no live lease, or "
                "only a settle hold waiting for this very host) and no updater is live here, "
                "so nothing is coming back to resume it; self-unpausing",
                paused_for,
            )
            unpause_local_cluster()
            # This is the automatic counterpart to `ava cluster recover`: the same
            # no-owner proof has matured past its safety bound and unpause succeeded.
            # Clear only afterwards so an unpause failure keeps the maintenance marker
            # honest and retryable rather than exposing a still-paused broken app.
            if snapshot.status == "updating" and snapshot.generation is not None:
                ui_update_state.clear(snapshot.generation)
            elif snapshot.status == "invalid":
                ui_update_state.force_clear()
            if handoff.generation is not None:
                updater_handoff.clear(handoff.generation)
            elif handoff.status == "invalid":
                updater_handoff.force_clear()
            if pause_snapshot.holder is not None and pause_snapshot.acquired_at is not None:
                pause_owner.clear(pause_snapshot.holder, pause_snapshot.acquired_at)
            elif pause_snapshot.status == "invalid":
                pause_owner.force_clear()
            return True
    except LockTimeoutError:
        # Expected cross-process contention during maintenance, not a daemon
        # error: another process (an update's UI write, the other watchdog)
        # holds the lifecycle mutex past its bounded wait. This round cannot
        # prove the pause is unowned, so recovery defers — the tick stays
        # blocked and the pause is kept exactly as it was. Never clear the
        # maintenance hold or unpause on missing evidence; other I/O failures
        # still raise through to the daemon.
        _log.warning(
            "[ops.pause] could not take the lifecycle lock; deferring stranded-pause "
            "recovery, pause kept"
        )
        return False


class PauseController:
    """Runs the pause gate as one manager controller. When paused, the tick is
    blocked either way (recovered-and-blocked, or still-paused-and-blocked); when
    unpaused, it does nothing and lets the tick proceed. Host-level — both
    capability watchdogs run it.

    Blocks with ``BlockScope.ALL``: a pause means a rollout deliberately took this
    host's services DOWN, every one of them, so nothing on the roster may be
    revived — unlike a schema/DB block, which is only about the DB's users."""

    name = "pause"
    timeout_s: float | None = None

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        # The gateway-capability watchdog maintains the reachability evidence the
        # owner determination consumes; a pure runner never accumulates it.
        if role == "gateway":
            record_gateway_reachability()
        if not is_paused():
            # Not paused: the stranded-hold verdict (task #3132) is definitionally
            # absent, so any record of it must go with it.
            sync_stranded_hold_record()
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        if recover_stranded_pause():
            return ReconcileResult(
                dimension=self.name,
                blocks=BlockScope.ALL,
                acted=True,
                detail="self-unpaused (stranded pause recovered)",
            )
        # Still paused and no recovery licensed: this is where a failed leg's
        # ownerless hold would otherwise sit silent and permanent. Record it —
        # and only record it; the no-auto-resume rule is untouched.
        sync_stranded_hold_record()
        _log.info("[ops.pause] host paused (posture), skipping tick")
        return ReconcileResult(dimension=self.name, blocks=BlockScope.ALL, detail="paused")
