"""The live consumer's stop-window gate, injected through `UnderstandingReadInputs.quiesced`."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from base.agents.history.hierarchy import chunk_consumer as loop
from base.agents.history.hierarchy.tests.consumer_helpers import read_inputs
from base.config import settings
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.event_capture import EmittedEvents


async def test_the_forever_loop_claims_nothing_while_the_host_is_quiesced(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    """A held unit's stop window is database-quiet; claiming resumes once it lifts."""
    claims = 0
    polls = 0

    async def claim_job(*_a: object, **_k: object) -> None:
        nonlocal claims
        claims += 1

    async def claim_rebuild(*_a: object, **_k: object) -> None:
        return None

    async def backlog(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(pending=0, running=0, oldest_pending_age_seconds=0.0)

    def quiesced() -> bool:
        nonlocal polls
        polls += 1
        return held

    held = True
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(loop, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(loop, "claim_job", claim_job)
    monkeypatch.setattr(loop, "claim_rebuild", claim_rebuild)
    monkeypatch.setattr(loop, "backlog", backlog)
    task = asyncio.create_task(
        loop.understanding_loop_forever(
            MagicMock(),
            MagicMock(),
            [],
            catalog=model_catalog,
            llm_override="",
            inputs=replace(read_inputs(), quiesced=quiesced),
        )
    )
    try:
        while polls < 3:
            await asyncio.sleep(0.01)
        assert claims == 0
        held = False
        while claims == 0:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
