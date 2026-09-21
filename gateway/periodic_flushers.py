"""Gateway-owned periodic flusher task construction."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from gateway import _agent_max_id, _auth401_log, _latency, completion_notice_flusher


def start(app: FastAPI) -> None:
    """Attach the gateway's singleton periodic tasks to its lifespan owner."""
    app.state.latency_flusher = asyncio.create_task(_latency.latency_flusher())
    app.state.auth401_flusher = asyncio.create_task(_auth401_log.auth401_flusher())
    app.state.agent_max_id_flusher = asyncio.create_task(
        _agent_max_id.max_agent_id_flusher(app.state.db_pool)
    )
    app.state.completion_notice_flusher = asyncio.create_task(
        completion_notice_flusher.completion_notice_flusher(app.state.db_pool)
    )
