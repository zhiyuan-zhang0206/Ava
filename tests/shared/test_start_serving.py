"""The per-start serving marker gates recovery actions until readiness succeeds."""

from __future__ import annotations

from pathlib import Path

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
