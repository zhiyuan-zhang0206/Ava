# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Generation-guarded persistent ownership of the gate maintenance page."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from shared import ui_update_state as state


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    marker = tmp_path / "deploy-state.json"
    monkeypatch.setattr(state, "state_path", lambda: marker)
    monkeypatch.setattr(state, "lock_path", lambda: tmp_path / "deploy-state.lock")
    return marker


def test_begin_and_phase_keep_one_generation_and_started_at(isolated: Path) -> None:
    opened = state.begin(kind="rollout", origin="frontend")
    assert opened.status == "updating"
    assert opened.schema_version == 2
    assert opened.generation is not None
    assert opened.started_at is not None

    assert state.set_phase(opened.generation, "phase-b") is True
    advanced = state.read()
    assert advanced.generation == opened.generation
    assert advanced.started_at == opened.started_at
    assert advanced.updated_at is not None
    assert advanced.updated_at >= opened.updated_at  # type: ignore[operator]
    assert advanced.phase == "phase-b"

    raw = json.loads(isolated.read_text())
    assert raw["posture"] == "paused", "old gates must read a v2 writer as updating"


def test_late_generation_cannot_overwrite_or_clear_the_new_owner(isolated: Path) -> None:
    first = state.begin(kind="rollout", origin="one")
    assert first.generation is not None
    assert state.clear(first.generation) is True
    second = state.begin(kind="restart", origin="two")
    assert second.generation is not None

    assert state.set_phase(first.generation, "late") is False
    assert state.clear(first.generation) is False
    current = state.read()
    assert current.generation == second.generation
    assert current.kind == "restart"


def test_active_generation_refuses_a_second_begin(isolated: Path) -> None:
    opened = state.begin(kind="rollout", origin="one")
    with pytest.raises(state.UiUpdateAlreadyActive, match=str(opened.generation)):
        state.begin(kind="restart", origin="two")


def test_begin_returns_the_generation_it_wrote_not_a_later_owner(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recover/new-begin between unlock and return must not hand caller A the
    identity of generation B; A's later CAS cleanup could otherwise delete B."""
    original_read = state.read

    def _replace_before_old_lockless_read(path: Path | str | None = None):
        monkeypatch.setattr(state, "read", original_read)
        state.force_clear()
        state.begin(kind="restart", origin="second")
        return original_read(path)

    monkeypatch.setattr(state, "read", _replace_before_old_lockless_read)

    first = state.begin(kind="rollout", origin="first")

    assert first.origin == "first"
    assert state.read().origin == "second"


def test_clear_removes_marker_and_host_transitions_do_not_recreate_it(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = state.begin(kind="rollout", origin="one")
    assert opened.generation is not None
    assert state.clear(opened.generation) is True
    assert state.read().status == "inactive"
    assert not isolated.exists()

    # Lock the ownership boundary mechanically without a real DB: host posture
    # code has no import/reference to the UI state owner.
    source = Path("shared/host_deploy_state.py").read_text()
    assert "from shared import ui_update_state" not in source
    assert "_write_mirror" not in source


@pytest.mark.parametrize("posture", ["paused", "converging"])
def test_new_reader_accepts_introducing_rollouts_v1_marker(isolated: Path, posture: str) -> None:
    isolated.write_text(json.dumps({"posture": posture, "updated_at": "2026-08-24T12:34:56+00:00"}))
    snap = state.read()
    assert snap.status == "updating"
    assert snap.legacy is True
    assert snap.started_at == dt.datetime(2026, 8, 24, 12, 34, 56, tzinfo=dt.UTC)


def test_legacy_idle_and_missing_are_inactive(isolated: Path) -> None:
    assert state.read().status == "inactive"
    isolated.write_text('{"posture":"idle","updated_at":"2026-08-24T12:34:56+00:00"}')
    assert state.read().status == "inactive"


@pytest.mark.parametrize(
    "raw",
    [
        "{not-json",
        '{"schema_version":99,"state":"updating"}',
        '{"schema_version":2,"state":"updating","generation":"g"}',
        '{"posture":"mystery","updated_at":"2026-08-24T12:34:56+00:00"}',
    ],
)
def test_corrupt_or_unknown_marker_is_explicitly_invalid(isolated: Path, raw: str) -> None:
    isolated.write_text(raw)
    snap = state.read()
    assert snap.status == "invalid"
    assert snap.error is not None


def test_replace_failure_before_commit_raises_without_a_marker(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("replace")))

    with pytest.raises(OSError, match="replace"):
        state.begin(kind="rollout", origin="one")

    assert not isolated.exists()


def test_directory_fsync_failure_after_replace_returns_committed_generation(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        state, "_fsync_parent", lambda _path: (_ for _ in ()).throw(OSError("dir fsync"))
    )

    opened = state.begin(kind="rollout", origin="one")

    assert opened.generation is not None
    assert state.read().generation == opened.generation


def test_directory_fsync_failure_after_unlink_reports_clear_success(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = state.begin(kind="rollout", origin="one")
    assert opened.generation is not None
    monkeypatch.setattr(
        state, "_fsync_parent", lambda _path: (_ for _ in ()).throw(OSError("dir fsync"))
    )

    assert state.clear(opened.generation) is True
    assert not isolated.exists()
