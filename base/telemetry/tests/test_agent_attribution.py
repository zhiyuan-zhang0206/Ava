"""Ordinary events retain explicit or process identity without turn attribution."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import replace

import pytest

import ava
from base import telemetry
from base.agents.context.identity import AgentIdentity


@pytest.fixture(autouse=True)
def restore_process_binding() -> Generator[telemetry.EventPipeline, None, None]:
    previous = telemetry.prepare_event("log", "log")
    pipeline = telemetry.EventPipeline(writer=lambda _batch: None)
    try:
        yield pipeline
    finally:
        telemetry.init_telemetry(
            process=previous.process, agent_id=previous.agent_id, pipeline=pipeline
        )
        pipeline.stop(timeout=2)


@pytest.mark.asyncio
async def test_concurrent_events_keep_explicit_or_process_identity(
    restore_process_binding: telemetry.EventPipeline,
) -> None:
    telemetry.init_telemetry(process="agent_host", agent_id=None, pipeline=restore_process_binding)
    ava.context = replace(ava.context, identity=AgentIdentity(99, True))

    async def record(agent_id: int):
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


def test_exec_process_identity_is_retained(
    restore_process_binding: telemetry.EventPipeline,
) -> None:
    telemetry.init_telemetry(process="agent-exec", agent_id=7, pipeline=restore_process_binding)
    ava.context = replace(ava.context, identity=AgentIdentity(42, True))
    assert telemetry.prepare_event("log", "log").agent_id == 7
