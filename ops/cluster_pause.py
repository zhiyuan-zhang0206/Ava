"""Drain local native work before stopping its dependencies.

The existing pause-owner journal closes admission and records checkpoint/exit
receipts. Gateway middleware reads the separate DB posture, which changes only
when the caller proceeds to service shutdown after the cluster-wide barrier.
"""

from __future__ import annotations

import logging
from typing import cast

import shared.host_deploy_state

_log = logging.getLogger(__name__)
_UNSET = object()


def is_paused(
    state: shared.host_deploy_state.HostDeployState | None | object = _UNSET,
) -> bool:
    """Whether this host is paused — the `host_deploy_state.posture` row written
    by the gateway's pause fan-out (R1, Task #1021).

    Gateway middleware checks this on every request. The row is read from the
    central DB, which the gateway owns; a read failure (DB unreachable) reads as
    NOT paused — the same conservative direction the old file stat had (an
    unreadable flag was an absent flag). The offline maintenance page is owned
    separately by the cluster orchestrator's Gate marker.
    """
    if state is _UNSET:
        try:
            resolved_state = shared.host_deploy_state.read()
        except Exception:
            _log.warning(
                "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
                exc_info=True,
            )
            return False
    else:
        resolved_state = cast(shared.host_deploy_state.HostDeployState | None, state)
    return resolved_state is not None and resolved_state.posture == "paused"


def pause_local_cluster() -> None:
    """Drain native agents while keeping their in-flight SDK dependencies available.

    The existing admission journal also keeps watchdogs and schedule admission
    paused. Posture becomes 503 only when the caller actually stops services,
    after all participating runners have completed their ordinary restarts.
    """
    from ops.agent_pause import pause_agents
    from shared.config import settings

    pause_agents(settings.gateway.update_quiesce_timeout_seconds)


def unpause_local_cluster() -> None:
    """Restore posture, then release this unit's existing agent pause."""
    from ops.agent_pause import resume_agents
    from shared import maintenance

    current = maintenance.snapshot()
    if current is None:
        _unpause_local_cluster()
        return
    if current.maintenance is not None and current.maintenance.failures:
        raise RuntimeError("cannot resume failed continuation/flush receipts; hold retained")
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    from shared import start_serving

    if (
        current.maintenance is not None
        and current.maintenance.phase in ("stopping", "stopped", "starting", "ready")
        and not start_serving.is_serving()
    ):
        raise RuntimeError("services have stopped; ava start must pass readiness before resume")
    with maintenance.authorized_start(current.holder, current.acquired_at):
        _unpause_local_cluster()
    resume_agents()


def _unpause_local_cluster() -> None:
    """Restore this unit's HTTP posture without launching any agent or service."""
    from shared import maintenance
    from shared.host_deploy_state import set_posture

    maintenance.require_start_allowed()
    set_posture("idle")
    _log.info("[cluster] unpaused: posture -> idle")


def finalize_pause_owner_journal() -> None:
    """Finalize only a legacy deploy journal after its caller restores service.

    Current continuation holds are released by resume_agents; the legacy CAS
    refuses every journal containing maintenance state.
    """
    from shared.pause_owner import finalize_natural_resume

    if finalize_natural_resume():
        _log.info("[cluster] legacy pause-owner journal: resumed")
