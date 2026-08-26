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


def test_finalize_natural_resume_records_the_journaled_generation_as_resumed() -> None:
    """A host that returns to serving on its own (Phase-B `ava start`, the
    gateway-local finally) must still record the journaled generation as
    `resumed` — otherwise the journal stays `paused` forever while the rollout
    finished (the 2026-08-26 residue: rollout rc=0, deploy-pause-owner.json
    still paused)."""
    pause_owner.mark_paused("gateway:pid1", _when())
    assert pause_owner.finalize_natural_resume() is True
    snapshot = pause_owner.read()
    assert snapshot.status == "resumed"
    assert snapshot.matches("gateway:pid1", _when())
    # Idempotent: a repeated finalize leaves the record alone.
    assert pause_owner.finalize_natural_resume() is False
    assert pause_owner.read().matches("gateway:pid1", _when())


def test_finalize_natural_resume_is_generation_scoped_and_never_force_clears() -> None:
    """The finalize must only ever transition a `paused` journal, and only to
    its own generation — an absent / legacy / resumed / invalid journal is left
    untouched (no generation-less force clearing), and a newer pause replaces
    the journal before a delayed finalize could touch it."""
    # inactive
    assert pause_owner.finalize_natural_resume() is False
    # invalid: stays invalid for recovery's no-live-owner proof
    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"A","acquired_at":"2026-08-25T01:02:00"}'
    )
    assert pause_owner.finalize_natural_resume() is False
    assert pause_owner.read().status == "invalid"
    # legacy tombstone: untouched
    pause_owner.force_clear()
    pause_owner.mark_legacy_resumed()
    assert pause_owner.finalize_natural_resume() is False
    assert pause_owner.read().status == "legacy-resumed"
    # a newer pause wins over a delayed finalize
    pause_owner.mark_paused("A", _when())
    pause_owner.mark_paused("B", _when(1))
    assert pause_owner.finalize_natural_resume() is True
    assert pause_owner.read().matches("B", _when(1))


def test_legacy_resume_tombstone_is_idempotent_and_exact_stop_replaces_it() -> None:
    assert pause_owner.mark_legacy_resumed().status == "legacy-resumed"
    assert pause_owner.mark_legacy_resumed().status == "legacy-resumed"
    pause_owner.mark_paused("B", _when())
    assert pause_owner.read().matches("B", _when())
