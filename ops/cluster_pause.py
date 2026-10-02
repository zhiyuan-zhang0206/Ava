"""Resume a drained unit and read or release its pause posture.

The drain itself is `ops.agent_pause`. The existing pause-owner journal closes
admission and records checkpoint/exit receipts. It fences HTTP only after the
cluster-wide drain barrier advances this home into its stop window. Database
posture remains a status projection.
"""

from __future__ import annotations

import logging
from typing import cast

import psycopg
from psycopg_pool import ConnectionPool

import base.deploy.state.host_deploy_state
from base.daemon.health import health_port
from base.deploy.maintenance.pause_owner import PauseOwnerSnapshot
from base.host.net import http_dial

_log = logging.getLogger(__name__)
_UNSET = object()

# Bound for the loopback dial that releases the host daemon's idle pool
# connections; the host answers in milliseconds (it only closes sockets), so
# this only guards a wedged or unreachable listener.
_POOL_RELEASE_TIMEOUT_S = 5.0


def is_paused(
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
            resolved_state = base.deploy.state.host_deploy_state.read()
        except Exception:
            _log.warning(
                "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
                exc_info=True,
            )
            return False
    else:
        resolved_state = cast(base.deploy.state.host_deploy_state.HostDeployState | None, state)
    return resolved_state is not None and resolved_state.posture == "paused"


def unpause_local_cluster() -> None:
    """Restore posture, then release this unit's existing agent pause."""
    from base.deploy.maintenance import admission
    from ops.agent_pause import resume_agents

    current = admission.snapshot()
    if current is None:
        _unpause_local_cluster()
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
        _unpause_local_cluster()
    resume_agents()


def _hold_refusal(current: PauseOwnerSnapshot) -> str | None:
    """The refusal `unpause_local_cluster` raises for a held unit, or None when its
    resume may proceed."""
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if current.maintenance is not None and current.maintenance.failures:
        return (
            "cannot resume failed continuation/flush receipts; fix the root cause, "
            f"then ava maintenance repair --operation {current.holder} "
            f"--acquired-at {current.acquired_at.isoformat()}"
        )
    from base.deploy.lifecycle import start_serving

    if (
        current.maintenance is not None
        and current.maintenance.phase in ("stopping", "stopped", "starting", "ready")
        and not start_serving.is_serving()
    ):
        return "services have stopped; ava start must pass readiness before resume"
    return None


def _unpause_local_cluster() -> None:
    """Restore this unit's HTTP posture without launching any agent or service."""
    from base.deploy.maintenance import admission
    from base.deploy.state.host_deploy_state import set_posture

    admission.require_start_allowed()
    set_posture("idle")
    _log.info("[cluster] unpaused: posture -> idle")


def release_local_db_pools(
    ops_pool: ConnectionPool[psycopg.Connection] | None,
) -> dict[str, object]:
    """Release this unit's idle DB-pool connections; never fail the stop.

    Two client pools survive the agent drain: the local host daemon's shared /
    control pools (dialed over its loopback health port) and the ops daemon's
    own dispatch pool, which that daemon passes in as `ops_pool` (None before
    its pool opens). Left open, they hold PgBouncer server connections
    through the data-plane window the stop is about to close. Both releases are
    best-effort — a failure leaves the stop correct but leaks idle client
    connections into the downtime — so it logs loudly and reports in the
    returned payload instead of raising.
    """
    released: dict[str, object] = {}

    try:
        resp = http_dial.post(
            f"http://127.0.0.1:{health_port('agent_host')}/release-db-pools",
            timeout=_POOL_RELEASE_TIMEOUT_S,
        )
        resp.raise_for_status()
        released["host"] = resp.json().get("released", {})
    except Exception as exc:
        _log.warning("[cluster] host pool release failed (continuing the stop): %s", exc)
        released["host_error"] = str(exc)

    try:
        if ops_pool is not None:
            from base.db.pool_release import release_idle_sync

            released["ops"] = release_idle_sync(ops_pool)
    except Exception as exc:
        _log.warning("[cluster] ops pool release failed (continuing the stop): %s", exc)
        released["ops_error"] = str(exc)

    return released
