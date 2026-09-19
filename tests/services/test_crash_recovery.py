"""The reap's guarded recovery attempt — `services/agent_host/crash_recovery.py` (task #4039).

The reaper commits the death's wake inside its terminating transaction; this
module is the attempt that consumes it right after. Every outcome a refusal,
a race, or a crash can produce is a deferral, not a loss: the wake row stays
pending for the delivery watchdog's terminated-owner retry. These lock the
call shape, the wake-less skip, and the never-raise discipline.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.corpse_reap import ReapedCorpse
from services.agent_host.crash_recovery import recover_reaped_corpses


async def test_attempts_the_guarded_resurrect_per_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str]] = []

    async def _attempt(agent_id: int, *, trigger_inbound_id: int, trigger_inbound_kind: str) -> str:
        calls.append((agent_id, trigger_inbound_id, trigger_inbound_kind))
        return "spawned"

    import ops.ops_lifecycle

    monkeypatch.setattr(ops.ops_lifecycle, "resurrect_if_terminated", _attempt)

    await recover_reaped_corpses([ReapedCorpse(7, 101), ReapedCorpse(8, 102)])

    assert calls == [(7, 101, "chat"), (8, 102, "chat")]


async def test_wake_less_entries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _attempt(*args: object, **kwargs: object) -> str:
        raise AssertionError("a wake-less entry must not reach the resurrect")

    import ops.ops_lifecycle

    monkeypatch.setattr(ops.ops_lifecycle, "resurrect_if_terminated", _attempt)

    await recover_reaped_corpses([])
    await recover_reaped_corpses([ReapedCorpse(7, None)])


async def test_an_attempt_failure_defers_without_stopping_the_next(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    attempted: list[int] = []

    async def _attempt(agent_id: int, *, trigger_inbound_id: int, trigger_inbound_kind: str) -> str:
        if trigger_inbound_id == 101:
            raise RuntimeError("resurrect dispatch exploded")
        attempted.append(agent_id)
        return "idling"

    import ops.ops_lifecycle

    monkeypatch.setattr(ops.ops_lifecycle, "resurrect_if_terminated", _attempt)

    await recover_reaped_corpses([ReapedCorpse(7, 101), ReapedCorpse(8, 102)])

    assert attempted == [8]
    deferred = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "crash_recovery_wake_deferred"
    ]
    assert [record["extra"]["agent_id"] for record in deferred] == [7]
