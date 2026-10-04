"""The marker contract of the home's generation-owned PTY allocation freeze.

The service's enforcement of the marker is covered by `services/agent_runner/pty_sessions/tests/test_freeze.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.sessions.pty import allocation_freeze


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def test_freeze_lives_in_the_home_and_records_operator_generation(_isolated_home: Path) -> None:
    assert allocation_freeze.state_path() == _isolated_home / "pty-allocation-freeze.json"
    assert allocation_freeze.lock_path() == _isolated_home / "pty-allocation.lock"

    frozen = allocation_freeze.freeze(holder="operator-1818", reason="bounded cleanup")

    assert frozen.status == "frozen"
    assert frozen.generation
    assert frozen.holder == "operator-1818"
    assert frozen.reason == "bounded cleanup"
    assert frozen.created_at is not None
    assert allocation_freeze.read() == frozen


def test_resume_is_generation_scoped_and_never_clears_a_newer_owner() -> None:
    first = allocation_freeze.freeze(holder="first", reason="first cleanup")
    assert first.generation is not None

    assert not allocation_freeze.resume("stale-generation")
    assert allocation_freeze.read() == first
    assert allocation_freeze.resume(first.generation)
    active = allocation_freeze.read()
    assert active.status == "inactive"
    assert active.generation == first.generation
    assert allocation_freeze.current_generation() == first.generation

    second = allocation_freeze.freeze(holder="second", reason="second cleanup")
    assert second.generation is not None and second.generation != first.generation
    assert not allocation_freeze.resume(first.generation)
    assert allocation_freeze.read() == second
