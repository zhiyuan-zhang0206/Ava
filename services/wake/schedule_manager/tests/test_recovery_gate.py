from __future__ import annotations

import pytest

from services.wake.schedule_manager.recovery_gate import RecoveryGate


def _ignore_held(_held_for_s: float) -> None:
    pass


def _ignore_refusal(_schedule_id: int, _reason: str) -> None:
    pass


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _gate(*, alert_after_s: float = 60.0, permitted: bool = False):
    clock = Clock()
    held: list[float] = []
    refused: list[tuple[int, str]] = []
    gate = RecoveryGate(
        alert_after_s=alert_after_s,
        emit_held=held.append,
        emit_refusal=lambda schedule_id, reason: refused.append((schedule_id, reason)),
        permitted=lambda: permitted,
        now=clock,
    )
    return gate, clock, held, refused


def test_a_short_gate_reports_nothing() -> None:
    gate, clock, held, refused = _gate(alert_after_s=60.0)
    gate.observe()
    clock.value += 10.0
    gate.observe()
    assert held == []
    assert refused == []


def test_a_sustained_gate_reports_once_per_episode() -> None:
    gate, clock, held, _ = _gate(alert_after_s=60.0)
    gate.observe()
    clock.value += 61.0
    gate.observe()
    clock.value += 600.0
    gate.observe()
    assert len(held) == 1
    assert held[0] == pytest.approx(61.0)


def test_the_next_episode_alerts_again() -> None:
    permitted = False
    clock = Clock()
    held: list[float] = []
    gate = RecoveryGate(
        alert_after_s=10.0,
        emit_held=held.append,
        emit_refusal=_ignore_refusal,
        permitted=lambda: permitted,
        now=clock,
    )
    gate.observe()
    clock.value += 11.0
    gate.observe()
    permitted = True
    gate.observe()
    permitted = False
    gate.observe()
    clock.value += 11.0
    gate.observe()
    assert len(held) == 2


def test_a_refusal_is_reported_once_per_schedule_per_episode() -> None:
    gate, _, _, refused = _gate()
    gate.observe()
    gate.refusal(7, "gated")
    gate.refusal(7, "gated")
    gate.refusal(9, "gated")
    assert refused == [(7, "gated"), (9, "gated")]


def test_lifting_the_gate_forgets_the_previous_episode() -> None:
    permitted = False
    refused: list[tuple[int, str]] = []
    gate = RecoveryGate(
        alert_after_s=10.0,
        emit_held=_ignore_held,
        emit_refusal=lambda schedule_id, reason: refused.append((schedule_id, reason)),
        permitted=lambda: permitted,
    )
    gate.observe()
    gate.refusal(7, "gated")
    permitted = True
    gate.observe()
    permitted = False
    gate.observe()
    gate.refusal(7, "gated")
    assert [schedule_id for schedule_id, _ in refused] == [7, 7]


def test_the_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RecoveryGate(
            alert_after_s=0.0,
            emit_held=_ignore_held,
            emit_refusal=_ignore_refusal,
        )
