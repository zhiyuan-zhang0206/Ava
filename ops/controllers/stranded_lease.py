"""Stranded-deploy-lease controller — reclaim the deploy lease of a dead holder.

A cluster update / rollback / restart holds the cluster deploy lease
(`shared.cluster_lock`, the single `deployment_state` row) for its whole run and
renews it while executing. A hard-killed orchestration cannot release it, and
the row then outlives its holder by up to `LOCK_TTL_S` (30 min): every new
deploy is refused — "another cluster update is in progress (held by ...:pidN)" —
on the strength of a process that provably no longer exists. `ava cluster
recover` breaks exactly that hold by hand, probing the holder pid; the
2026-09-12 dev-worktree incident is what the gap looks like unattended (a
rollback died mid-start leaving a stranded lease + pause; a human cleared it
9.5 minutes later). This controller is the automatic counterpart: one watchdog
round reclaims the row once the holder is PROVABLY gone.

Narrow by construction:

- **Only a plain executing lease** (`note is None`). A settle hold's entire
  purpose is to outlive its writer — it is a stated waiting period, released by
  convergence or its own `SETTLE_TTL_S` — so it is never touched here.
- **Only positive death evidence** (`shared.cluster_lock.holder_process_gone`):
  the holder names THIS machine and its pid is absent (or, aged, provably
  recycled). Another machine's holder, an unparseable string, and an unreadable
  process identity all read as live — the same conservative contract as
  `ops.ops_cluster._lock_holder_is_live` (its negation), which the manual
  recovery keeps using.
- **A live local orchestration session or a live local updater lease declines**:
  a fresh owner may be about to claim the row, and the manual op refuses there
  too.
- **The clear is a compare-and-set on the exact lease observed**
  (`claim_recovery_lock`): a holder that lands mid-tick is never clobbered; the
  reclaim is attributed (`recovery:<machine>:pid<N>`) and released, leaving the
  row free for the next deploy.

What it deliberately does NOT do: touch the paused posture or any maintenance
hold. A hold stays hand-recovered (`conventions/graceful-maintenance.md`,
"Recovering a stuck maintenance operation"); reclaiming the lease only removes
the deployability block, so the sanctioned recovery — an operator, `ava start`,
or the next rollout — is no longer refused by a corpse's lease.

Runs ahead of `PauseController` in the manager's order: a rollout pauses its
hosts before it stops/restarts them, so this controller meets its case on
paused hosts, and anything behind `PauseController` is short-circuited away
while the pause blocks. Host-level; both capability watchdogs run it (a dead
holder can be any local process).
"""

from __future__ import annotations

import logging
import os

from ops import cluster_session
from ops.controllers.base import BlockScope, ReconcileResult
from shared import ui_update_state
from shared.cluster_lock import (
    DeployLease,
    claim_recovery_lock,
    holder_process_gone,
    read_update_lease,
    release_update_lock,
)
from shared.host_deploy_state import updater_lease_live
from shared.machine import MachineRole, machine_name
from shared.platform import LockTimeoutError

_log = logging.getLogger("ops.controllers.stranded_lease")


def dead_deploy_lease(lease: DeployLease) -> bool:
    """Whether `lease` is a plain executing lease whose holder is provably gone."""
    return lease.note is None and holder_process_gone(lease.holder, held_for_s=lease.held_for_s)


def reclaim_dead_deploy_lease() -> str | None:
    """Reclaim the deploy lease when its holder is provably gone; else None.

    Returns the reclaimed holder string. Every refusal — not a plain lease, not
    provably gone, a live session/updater, a lost CAS, a transient read failure —
    logs and returns None; a controller must degrade, never raise on a flaky
    round."""
    try:
        lease = read_update_lease()
    except Exception:
        _log.warning("[ops.lease] could not read the deploy lease; skipping this round")
        return None
    if lease is None or not dead_deploy_lease(lease):
        return None
    if cluster_session.live_orchestration_session() is not None:
        _log.warning(
            "[ops.lease] deploy lease holder %s is provably gone, but a local orchestration "
            "session is alive — deferring the reclaim to it",
            lease.holder,
        )
        return None
    if updater_lease_live():
        _log.warning(
            "[ops.lease] deploy lease holder %s is provably gone, but an updater is live on "
            "this host — deferring the reclaim",
            lease.holder,
        )
        return None
    recovery_holder = f"recovery:{machine_name()}:pid{os.getpid()}"
    try:
        with ui_update_state.lifecycle_lock():
            claim = claim_recovery_lock(recovery_holder, lease)
            if not claim.acquired:
                _log.warning(
                    "[ops.lease] the deploy lease changed while its holder's death was being "
                    "proven — a new owner may have started; reclaim refused"
                )
                return None
            try:
                _log.warning(
                    "[ops.lease] reclaimed the deploy lease of a provably dead holder (%s); "
                    "held as %s",
                    lease.holder,
                    recovery_holder,
                )
            finally:
                release_update_lock(recovery_holder)
    except LockTimeoutError:
        _log.warning("[ops.lease] could not take the lifecycle lock; deferring the reclaim")
        return None
    return lease.holder


class StrandedLeaseController:
    """Runs the dead-holder lease reclaim as one manager controller.

    Never blocks: the reclaim is a narrow, fully-gated write, and rounds it
    declines must reach the controllers behind it unchanged. Host-level — the
    holder can be any local process (a gateway orchestration or a local
    rollback), so both capability watchdogs run it.
    """

    name = "lease"
    timeout_s: float | None = None

    def reconcile(self, role: MachineRole) -> ReconcileResult:  # noqa: ARG002 — host-level, uniform Controller signature
        reclaimed = reclaim_dead_deploy_lease()
        return ReconcileResult(
            dimension=self.name,
            blocks=BlockScope.NONE,
            acted=reclaimed is not None,
            detail=(
                f"reclaimed the deploy lease of a dead holder ({reclaimed})" if reclaimed else None
            ),
        )
