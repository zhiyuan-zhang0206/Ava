"""Versioned ACTIVE restart executor over its dedicated transactional receipt."""

import asyncio

from psycopg_pool import ConnectionPool

from base.agents.incarnation.native_restart_models import (
    NativeRestartAccepted,
    NativeRestartOperation,
    NativeRestartOutcome,
    NativeRestartRefused,
    NativeRestartRequest,
)
from base.agents.messages.native_restart import (
    NativeRestartConflictError,
    accept_native_restart,
    native_restart_progress,
)
from base.db import Database, publish_inbound_wake
from base.events.live.bus import EventBus
from base.lm.registry import normalize_overlay_llm_model
from base.log import logger
from ops.rpc_schemas import RestartAgentRequest


class NativeRestartOverlayError(ValueError):
    """Fresh overlay validation failed before source or configuration effects."""


def _freeze_overlay(request: NativeRestartRequest) -> dict[str, object] | None:
    try:
        options = RestartAgentRequest.model_validate(
            {"source": request.source, "config_overlay": request.config_overlay}
        )
        overlay = dict(options.config_overlay) if options.config_overlay else None
        if overlay:
            normalize_overlay_llm_model(overlay)
        return overlay
    except ValueError as exc:
        raise NativeRestartOverlayError("native restart overlay is invalid") from exc


async def restart_native_work_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    operation: NativeRestartOperation,
    pool: ConnectionPool,
) -> NativeRestartAccepted | NativeRestartRefused:
    """Acceptance is its own protocol; no generic claim/result cache owns replay."""
    if not 0 < agent_id < 2**63:
        raise ValueError("guarded restart agent id is outside positive BIGINT")
    try:
        acceptance = await asyncio.to_thread(
            accept_native_restart,
            pool,
            operation.operation_key,
            agent_id,
            operation.request,
            _freeze_overlay,
        )
    except NativeRestartConflictError as exc:
        return NativeRestartRefused(status="refused", reason="identity_conflict", detail=str(exc))
    except NativeRestartOverlayError as exc:
        return NativeRestartRefused(status="refused", reason="invalid_overlay", detail=str(exc))
    progress = await asyncio.to_thread(
        native_restart_progress, pool, agent_id, acceptance.command_id
    )
    if progress is not None and progress.outcome == NativeRestartOutcome.ACCEPTED:
        try:
            await asyncio.to_thread(
                publish_inbound_wake, db, bus, agent_id, str(acceptance.command_id)
            )
        except Exception:
            logger.exception("guarded restart acceptance lost its live hint")
    return NativeRestartAccepted(status="accepted", acceptance=acceptance)
