"""Hosted boot recovery deferral signal (task #3619).

Every boot that defers an agent's recovery emits the
``hosted_boot_recovery_deferred`` anomaly event; repeats across boots are
counted by the alert rule over the event stream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from base.agents.incarnation.exec_request_evidence import RequestEvidence, Verdict
from services.agent_runner.agent_host import daemon

_AGENT = 4242


def _evidence(agent_id: int) -> RequestEvidence:
    return RequestEvidence(
        agent_id=agent_id,
        path=Path("/nonexistent-exec") / f"req-{agent_id}.json",
        verdict=Verdict.UNKNOWN,
        incarnation=None,
        mtime=0.0,
        live_pids=(),
        detail="retained evidence",
    )


async def test_every_boot_that_defers_an_agent_emits_the_deferred_event(
    loguru_records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred = {_AGENT: (_evidence(_AGENT),)}

    async def recover(
        _pool: Any, _machine: str, **_kwargs: Any
    ) -> tuple[list[int], dict[int, tuple[Any, ...]]]:
        return [], deferred

    monkeypatch.setattr(daemon, "recover_orphaned_hosted_forces", recover)
    for _ in range(2):
        await daemon._recover_hosted_forces_at_boot(Mock(), "machine-x")

    events = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "hosted_boot_recovery_deferred"
    ]
    assert len(events) == 2
    assert all(record["level"].name == "WARNING" for record in events)
    extra = events[0]["extra"]
    assert extra["agent_id"] == _AGENT
    assert str(_AGENT) in extra["evidence"] and "--agent" in extra["hint"]


async def test_a_boot_with_no_deferral_emits_nothing(
    loguru_records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def settle_all(
        _pool: Any, _machine: str, **_kwargs: Any
    ) -> tuple[list[int], dict[int, tuple[Any, ...]]]:
        return [_AGENT], {}

    monkeypatch.setattr(daemon, "recover_orphaned_hosted_forces", settle_all)
    await daemon._recover_hosted_forces_at_boot(Mock(), "machine-x")
    assert not [
        r for r in loguru_records if r["extra"].get("event") == "hosted_boot_recovery_deferred"
    ]
