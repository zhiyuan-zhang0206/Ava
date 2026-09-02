"""The per-start serving marker gates recovery actions until readiness succeeds."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest


@pytest.fixture
def state_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    return path


def test_new_start_attempt_invalidates_a_previous_serving_generation(state_path: Path) -> None:
    """A stale success marker cannot admit revival during a new failed boot.

    Replacing the matching-generation check with an unconditional serving write
    would let an earlier start mark the later attempt serving and fail this test.
    """
    from shared import start_serving

    previous = start_serving.begin_start()
    assert start_serving.mark_serving(previous) is True
    assert start_serving.is_serving() is True

    current = start_serving.begin_start()

    assert start_serving.is_serving() is False
    assert start_serving.mark_serving(previous) is False
    assert start_serving.mark_serving(current) is True
    assert start_serving.is_serving() is True


def test_clear_serving_keeps_revival_blocked(state_path: Path) -> None:
    """Stopping a host removes its authority to revive work."""
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True

    start_serving.clear_serving()

    assert start_serving.is_serving() is False


def test_start_waits_for_an_authorized_recovery_action(state_path: Path) -> None:
    """A new start cannot close the gate between authorization and revival.

    Removing the lock held by ``recovery_permitted`` would let the second
    thread finish ``begin_start`` before the action completes, reproducing the
    pre-readiness check-then-act race.
    """
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True
    recovery_authorized = Event()
    release_recovery = Event()
    start_completed = Event()

    def recover() -> None:
        with start_serving.recovery_permitted() as permitted:
            assert permitted is True
            recovery_authorized.set()
            assert release_recovery.wait(timeout=2)

    def start_again() -> None:
        start_serving.begin_start()
        start_completed.set()

    recovery_thread = Thread(target=recover)
    recovery_thread.start()
    assert recovery_authorized.wait(timeout=2)
    start_thread = Thread(target=start_again)
    start_thread.start()

    assert start_completed.wait(timeout=0.1) is False
    release_recovery.set()
    recovery_thread.join(timeout=2)
    start_thread.join(timeout=2)
    assert start_completed.is_set()
    assert start_serving.is_serving() is False
