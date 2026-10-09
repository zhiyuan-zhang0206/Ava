"""Ordinary events retain explicit or process identity without turn attribution."""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest

from base import telemetry
from base.native_process.turn_identity import bind_turn_identity


@pytest.fixture(autouse=True)
def restore_process_binding() -> Generator[None, None, None]:
    previous = telemetry.prepare_event("log", "log")
    try:
        yield
    finally:
        telemetry.init_telemetry(process=previous.process, agent_id=previous.agent_id)


@pytest.mark.asyncio
async def test_concurrent_events_keep_explicit_or_process_identity() -> None:
    telemetry.init_telemetry(process="agent_host", agent_id=None)

    async def record(agent_id: int):
        with bind_turn_identity(agent_id):
            await asyncio.sleep(0)
            return (
                telemetry.prepare_event("log", "log"),
                telemetry.prepare_event("log", "log", agent_id=agent_id),
            )

    records = await asyncio.gather(record(7), record(42))
    for agent_id, (ordinary, explicit) in zip((7, 42), records, strict=True):
        assert ordinary.agent_id is None
        assert ordinary.source == "system"
        assert explicit.agent_id == agent_id
        assert ordinary.machine == explicit.machine
        assert ordinary.machine
        assert ordinary.process == explicit.process == "agent_host"


def test_exec_process_identity_is_retained() -> None:
    telemetry.init_telemetry(process="agent-exec", agent_id=7)
    with bind_turn_identity(42):
        assert telemetry.prepare_event("log", "log").agent_id == 7
