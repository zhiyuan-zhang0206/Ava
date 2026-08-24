from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from shared import pause_owner


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "owner.lock")


def _when(second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 25, 1, 2, second, tzinfo=dt.UTC)


def test_pause_resume_is_an_exact_idempotent_capability() -> None:
    pause_owner.mark_paused("gateway:pid1", _when())
    assert not pause_owner.mark_resumed("gateway:pid1", _when(1))
    assert pause_owner.mark_resumed("gateway:pid1", _when())
    assert pause_owner.mark_resumed("gateway:pid1", _when())
    assert pause_owner.read().status == "resumed"


def test_new_stop_replaces_resumed_owner_and_late_resume_cannot_touch_it() -> None:
    pause_owner.mark_paused("A", _when())
    assert pause_owner.mark_resumed("A", _when())
    pause_owner.mark_paused("B", _when(1))
    assert not pause_owner.mark_resumed("A", _when())
    assert pause_owner.read().matches("B", _when(1))


def test_naive_or_malformed_identity_is_invalid() -> None:
    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"A","acquired_at":"2026-08-25T01:02:00"}'
    )
    assert pause_owner.read().status == "invalid"


def test_legacy_resume_tombstone_is_idempotent_and_exact_stop_replaces_it() -> None:
    assert pause_owner.mark_legacy_resumed().status == "legacy-resumed"
    assert pause_owner.mark_legacy_resumed().status == "legacy-resumed"
    pause_owner.mark_paused("B", _when())
    assert pause_owner.read().matches("B", _when())
