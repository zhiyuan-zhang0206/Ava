"""Drain local native work before stopping its dependencies.

The existing pause-owner journal closes admission and records checkpoint/exit
receipts. Gateway middleware reads the separate DB posture, which changes only
when the caller proceeds to service shutdown after the cluster-wide barrier.
"""

from __future__ import annotations

import logging
from typing import cast

import shared.host_deploy_state
from shared import http_dial
from shared.daemon_health import health_port
from shared.pause_owner import PauseOwnerSnapshot

_log = logging.getLogger(__name__)
_UNSET = object()

# Bound for the loopback dial that releases the host daemon's idle pool
# connections; the host answers in milliseconds (it only closes sockets), so
# this only guards a wedged or unreachable listener.
_POOL_RELEASE_TIMEOUT_S = 5.0


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
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if (refusal := _hold_refusal(current)) is not None:
        raise RuntimeError(refusal)
    if current.maintenance is not None and current.maintenance.undelivered:
        _log.warning(
            "[cluster] releasing with undelivered crash-equivalent receipts; "
            "cold admission re-drives their continuations: %s",
            sorted(current.maintenance.undelivered),
        )
    with maintenance.authorized_start(current.holder, current.acquired_at):
        _unpause_local_cluster()
    resume_agents()


def _hold_refusal(current: PauseOwnerSnapshot) -> str | None:
    """The refusal `unpause_local_cluster` raises for a held unit, or None when its
    resume may proceed. One source for both the pre-check a caller runs before
    attempting and the text the attempt itself raises, so they cannot disagree."""
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if current.maintenance is not None and current.maintenance.failures:
        return (
            "cannot resume failed continuation/flush receipts; fix the root cause, "
            f"then ava maintenance repair --operation {current.holder} "
            f"--acquired-at {current.acquired_at.isoformat()}"
        )
    from shared import start_serving

    if (
        current.maintenance is not None
        and current.maintenance.phase in ("stopping", "stopped", "starting", "ready")
        and not start_serving.is_serving()
    ):
        return "services have stopped; ava start must pass readiness before resume"
    return None


def local_resume_refusal() -> str | None:
    """Why this unit's local unpause cannot succeed right now, or None when it may.

    Read-only and local (pause-owner journal + start-serving marker), so it answers
    with every service down — exactly the state a caller most needs it in. A refusal
    is durable state no compensation attempt can clear; asking first turns the
    compensating unpause's two doomed attempts (issue #2162: `rc=1`, then an
    unreadable `RuntimeError`) into the one recovery command that fits.
    """
    from shared import maintenance

    try:
        current = maintenance.snapshot()
    except RuntimeError as exc:
        # An unreadable owner refuses new work; that refusal is the answer.
        return str(exc)
    if current is None:
        return None
    return _hold_refusal(current)


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


def release_local_db_pools() -> dict[str, object]:
    """Release this unit's idle DB-pool connections; never fail the stop.

    Two client pools survive the agent drain: the local host daemon's shared /
    control pools (dialed over its loopback health port) and this ops daemon's
    own dispatch pool. Left open, they hold PgBouncer server connections
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
        from services.agent_ops import daemon as ops_daemon

        pool = ops_daemon._db_pool
        if pool is not None:
            from shared.pool_release import release_idle_sync

            released["ops"] = release_idle_sync(pool)
    except Exception as exc:
        _log.warning("[cluster] ops pool release failed (continuing the stop): %s", exc)
        released["ops_error"] = str(exc)

    return released
