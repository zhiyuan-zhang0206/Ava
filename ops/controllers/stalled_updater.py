"""Stalled-updater controller — reconcile this host's `ava-updater` session against
the fact that a deploy is supposed to *end*.

Desired state (from Spec): the detached `ava-updater` session exists only while an
update is running. A session whose log has not advanced in
`NO_PROGRESS_TIMEOUT_S` is not running an update — it is a corpse holding the
name, and `ops.cluster_deploy.reap_stalled_updater_if_hung` kills it.

## Why this is a controller and not a caller-side check

The kill itself already existed, inside `spawn_update`: a new update arriving on
this host would find the session, judge it hung, reap it and take over. That reads
like enough — the next attempt clears it — and it is not, because **the corpse
refuses the next attempt before it can reach the reap**:

- `current_orchestration()` answers `"update"` for as long as the session is alive.
- The gateway's next `ava cluster update` asks `ops.deploy_window.deploy_in_flight()`,
  whose signal 3 reads every machine's `host_deploy_state` posture row (R1,
  Task #1021 — the old per-host probe died with the daemon it dialed) and reports
  this host as mid-deploy. `_assert_no_orchestration_in_flight` then refuses the
  whole rollout, cluster-wide, so Phase B never fans out and the `spawn_update`
  carrying the reap is never called.
- This host's **code**, **pin** and **pause** controllers read that same signal and
  defer to it, on the correct general rule that stale processes, an off-pin checkout
  and a set pause flag are all a running deploy's normal transients. Every one of
  them therefore waits on the corpse.

  Pin and pause used to gate on the `update_lock_holder()` lease alone, which a
  watchdog-spawned `spawn_update` never takes — so an off-pin host walked through to
  `spawn_update_local`, whose own inline reap cleared the corpse, and a stranded pause
  lifted on its timer. That is no longer true and its absence was not an escape hatch:
  the same blindness let the pin controller force-checkout underneath a *live*
  updater, flapping prod between two commits for two hours (issue #1074). The three
  now ask the same question, and this controller is what keeps the answer honest.

So one hung session on one runner permanently refuses the cluster's deploy path *and*
that runner's own self-heals, and the documented exits are `--force` — which skips
the cluster-wide deploy-window check the corpse is impersonating, i.e. teaches the
operator to suppress the real protection — or a human killing the updater's pid.
Bounding the Phase-B poll (the reason the previous behaviour was tolerable) does not
reach this: the poll protects a rollout that *started*, and this refuses the start.

## Why it runs first

The round is ordered pause -> ... , and `PauseController` blocks with
`BlockScope.ALL` while the host is paused (posture row) — which is exactly the state a hung
updater leaves behind, since `spawn_update` pauses the host before it spawns.
A reaper placed after the pause controller would be short-circuited away in
precisely the case it exists for. It is safe first because it never blocks: it
removes a false signal, it does not stop or start a service, so the controllers
behind it see an accurate host and decide as they always would.

## What it does not do

It does not unpause, restart or re-trigger anything. Killing the session hands the
host back to the controllers that already own those dimensions — stranded-pause lifts
the pause, pin force-updates the checkout, code restarts stale processes — all three
of which are unreachable while the corpse holds the session name. The narrow action is
the point: the unattended power this adds is "kill one session that has been silent
for 15 minutes", not "recover a host".

The evidence bar is unchanged from the caller-side reap — the updater
lease expired (R1, Task #1021; the same no-progress family as
`_UPDATER_STALL_TIMEOUT_S`) — so this is a new clock asking an existing
question, not a new judgement about when a deploy is dead.
"""

from __future__ import annotations

import logging

from ops.cluster import REAP_CLEARED_QUALIFIER
from ops.controllers.base import BlockScope, ReconcileResult
from shared.machine import MachineRole

_log = logging.getLogger("ops.controllers.stalled_updater")


class StalledUpdaterController:
    """Runs the hung-updater reap as one manager controller.

    Agent-runner only, and that is a co-location fact rather than a policy: the
    `ops` daemon is agent-runner-scoped, `spawn_update` runs in it, so every host
    that can hold an `ava-updater` session also runs an agent-runner watchdog. A
    gateway-only unit has no ops daemon and therefore no session to reap; adding the
    gateway leg would be a no-op on a split deployment and a second killer racing
    the first on a single box, which runs both watchdogs.
    """

    name = "updater"

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        if role != "agent-runner":
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        from ops.cluster import reap_stalled_updater_if_hung

        acted = reap_stalled_updater_if_hung()
        if acted:
            # "cleared", not "killed": the reap also reports success when the session
            # turned out to be gone on the recheck after a failed kill, and claiming
            # a kill there would be a log that names an action nobody took. The
            # qualifier is `cluster_deploy`'s, not this file's, so this call site and
            # `spawn_update`'s inline reap cannot drift into disagreeing about what
            # the same True means.
            _log.warning(
                "[ops.updater] cleared a hung ava-updater session (%s) — the host's own "
                "code reconcile and the cluster's next deploy were both deferring to it",
                REAP_CLEARED_QUALIFIER,
            )
        # Never blocks. The reap removes a stale signal; it takes nothing down, so
        # the controllers behind it must run this round on the corrected reading.
        return ReconcileResult(
            dimension=self.name,
            blocks=BlockScope.NONE,
            acted=acted,
            detail="cleared a hung ava-updater session" if acted else None,
        )
