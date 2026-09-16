"""Consecutive-boot deferral streaks and their escalation (task #3619).

Hosted boot recovery may defer for an agent whose exec evidence is not yet
disposable. One warning per boot rots silently, so the streak of consecutive
boots is persisted host-locally (no schema migration) and the third boot
escalates to the counted ``hosted_boot_recovery_stalled`` anomaly event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from loguru import logger

from services.agent_host import boot_defer, daemon
from shared.exec_request_evidence import RequestEvidence, Verdict

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


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "hosted-boot-recovery-defers.json"
    monkeypatch.setattr(boot_defer, "state_path", lambda: state)
    return state


def test_streaks_count_consecutive_boots_and_drop_settled_agents(ledger: Path) -> None:
    assert boot_defer.record_deferrals({7}) == {7: 1}
    assert boot_defer.record_deferrals({7, 9}) == {7: 2, 9: 1}
    # agent 9 settled (or vanished): only its streak ends; 7 continues.
    assert boot_defer.record_deferrals({7}) == {7: 3}
    assert json.loads(ledger.read_text(encoding="utf-8")) == {"7": 3}

    # a clean boot clears the ledger.
    assert boot_defer.record_deferrals(set()) == {}
    assert json.loads(ledger.read_text(encoding="utf-8")) == {}


def test_damaged_ledger_resets_instead_of_failing_the_boot(ledger: Path) -> None:
    ledger.write_text("{not json", encoding="utf-8")
    assert boot_defer.read_streaks() == {}

    ledger.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert boot_defer.read_streaks() == {}

    ledger.write_text(json.dumps({"7": -1, "x": 2, "9": 4}), encoding="utf-8")
    assert boot_defer.read_streaks() == {9: 4}


def test_unreadable_ledger_resets_and_the_next_deferral_starts_at_one(ledger: Path) -> None:
    ledger.write_text("{not json", encoding="utf-8")
    assert boot_defer.record_deferrals({7}) == {7: 1}


async def test_recovery_deferral_escalates_on_the_third_consecutive_boot(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred = {_AGENT: (_evidence(_AGENT),)}

    async def recover(_pool: Any, _machine: str) -> tuple[list[int], dict[int, tuple[Any, ...]]]:
        return [], deferred

    monkeypatch.setattr(daemon, "recover_orphaned_hosted_forces", recover)
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="WARNING", format="{message}"
    )
    try:
        for _ in range(boot_defer.ALERT_AFTER_BOOTS):
            await daemon._recover_hosted_forces_at_boot(Mock(), "machine-x")
    finally:
        logger.remove(sink)

    boots = [
        record
        for record in records
        if record["extra"].get("agent_id") == _AGENT and record["extra"].get("streak") is not None
    ]
    assert [record["extra"]["streak"] for record in boots] == [1, 2, 3]
    assert [record["extra"].get("event") for record in boots] == [
        None,
        None,
        "hosted_boot_recovery_stalled",
    ]
    escalated = boots[-1]["extra"]
    assert str(_AGENT) in escalated["evidence"] and "--agent" in escalated["hint"]


async def test_a_boot_with_no_deferral_clears_the_streak(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery ends the streak: the count restarts from the next deferral."""

    async def settle_all(_pool: Any, _machine: str) -> tuple[list[int], dict[int, tuple[Any, ...]]]:
        return [_AGENT], {}

    monkeypatch.setattr(daemon, "recover_orphaned_hosted_forces", settle_all)

    assert boot_defer.record_deferrals({_AGENT, 8}) == {8: 1, _AGENT: 1}
    await daemon._recover_hosted_forces_at_boot(Mock(), "machine-x")
    assert boot_defer.read_streaks() == {}
    assert boot_defer.record_deferrals({_AGENT}) == {_AGENT: 1}
