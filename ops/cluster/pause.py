"""Resume a drained unit and read or release its pause posture.

The drain itself is `ops.agent_pause`. The existing pause-owner journal closes
admission and records checkpoint/exit receipts. It fences HTTP only after the
cluster-wide drain barrier advances this home into its stop window. Database
posture remains a status projection.
"""

from __future__ import annotations

import logging
from typing import cast

import base.deploy.state.host_deploy_state
from base.db import Database
from base.deploy.maintenance.pause_owner import PauseOwnerSnapshot
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus

_log = logging.getLogger(__name__)
_UNSET = object()


def is_paused(
    db: Database,
    state: base.deploy.state.host_deploy_state.HostDeployState | object | None = _UNSET,
) -> bool:
    """Whether this host is paused — the `host_deploy_state.posture` row written
    by the gateway's pause fan-out (R1, Task #1021).

    Status readers consume this database projection. HTTP admission instead reads
    the local journal. A projection read failure (DB unreachable) reads as
    NOT paused — the same conservative direction the old file stat had (an
    unreadable flag was an absent flag).
    """
    if state is _UNSET:
        try:
            resolved_state = base.deploy.state.host_deploy_state.read(db)
        except Exception:
            _log.warning(
                "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
                exc_info=True,
            )
            return False
    else:
        resolved_state = cast(base.deploy.state.host_deploy_state.HostDeployState | None, state)
    return resolved_state is not None and resolved_state.posture == "paused"


def unpause_local_cluster(db: Database, bus: EventBus) -> None:
    """Restore posture, then release this unit's existing agent pause."""
    from base.deploy.maintenance import admission
    from ops.agent_pause import resume_agents

    current = admission.snapshot()
    if current is None:
        _unpause_local_cluster(db)
        return
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if (refusal := _hold_refusal(current)) is not None:
        raise RuntimeError(refusal)
    if current.maintenance is not None and current.maintenance.undelivered:
        _log.warning(
            "[cluster] releasing with undelivered crash-equivalent receipts; "
            "cold admission re-drives their continuations: %s",
            sorted(current.maintenance.undelivered),
        )
    with admission.authorized_start(current.holder, current.acquired_at):
        _unpause_local_cluster(db)
    resume_agents(db, bus)


def _hold_refusal(current: PauseOwnerSnapshot) -> str | None:
    """The refusal `unpause_local_cluster` raises for a held unit, or None when its
    resume may proceed."""
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if current.maintenance is not None and current.maintenance.failures:
        return (
            "cannot resume failed continuation/flush receipts; run `ava start`, which "
            "re-delivers them before it releases the hold"
        )
    from base.deploy.lifecycle import start_serving

    if (
        current.maintenance is not None
        and current.maintenance.phase
        in (
            MaintenancePhase.STOPPING,
            MaintenancePhase.STOPPED,
            MaintenancePhase.STARTING,
            MaintenancePhase.READY,
        )
        and not start_serving.is_serving()
    ):
        return "services have stopped; ava start must pass readiness before resume"
    return None


def _unpause_local_cluster(db: Database) -> None:
    """Restore this unit's HTTP posture without launching any agent or service."""
    from base.deploy.maintenance import admission
    from base.deploy.state.host_deploy_state import set_posture

    admission.require_start_allowed()
    set_posture(db, "idle")
    _log.info("[cluster] unpaused: posture -> idle")
