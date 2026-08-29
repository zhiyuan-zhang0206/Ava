"""Health projection for the PITR base scheduler."""

from __future__ import annotations

import time
from typing import Any

from services.pitr.activation_runtime import activation_health_component
from services.pitr.retention_scheduler import health_component as retention_health_component
from shared.health_schema import DEGRADED, OK, component

BASE_BACKUP_STALE_AFTER_S = 8 * 24 * 60 * 60


def components(state: Any) -> list[dict[str, object]]:
    if state.cleanup_pending:
        status, detail = DEGRADED, state.last_error or "completed candidate cleanup is pending"
        progress = "cleanup"
    elif state.running:
        status, detail = OK, None
        progress = "running"
    elif state.last_error:
        status, detail = DEGRADED, state.last_error
        progress = "idle"
    elif state.last_success and time.time() - state.last_success > BASE_BACKUP_STALE_AFTER_S:
        status, detail = DEGRADED, "last base candidate is older than eight days"
        progress = "idle"
    else:
        status, detail, progress = OK, None, "idle"
    # The candidate's state is a domain condition (cleanup pending, GCS
    # credentials, replication contract, staleness) that a restart cannot
    # fix; gating readiness would make the watchdog respawn a healthy daemon
    # every 60s onto the same condition (QA #931 R3, #927 arbitration A).
    # Readiness follows process liveness only — /healthz 503 means the
    # daemon is dead.
    record = component(
        "pitr_base_candidate",
        status,
        last_success=state.last_success,
        progress=progress,
        detail=detail,
        now=time.time() if state.last_success else None,
        gate_readiness=False,
    )
    record["protected"] = False
    record["deferred_for_logical_backup"] = state.deferred_for_logical_backup
    record["cleanup_pending"] = state.cleanup_pending
    restore_status = OK if state.last_protected or not state.last_error else DEGRADED
    restore = component(
        "pitr_restore_proof",
        restore_status,
        last_success=state.last_protected,
        progress="running" if state.restore_running else "idle",
        detail=state.last_error if restore_status == DEGRADED else None,
        now=time.time() if state.last_protected else None,
        gate_readiness=False,
    )
    restore["protected"] = state.last_protected is not None
    return [
        record,
        restore,
        retention_health_component(state.retention),
        activation_health_component(),
    ]
