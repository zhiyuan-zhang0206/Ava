from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from base.deploy.maintenance import pause_owner


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "owner.lock")


def _when(second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 25, 1, 2, second, tzinfo=dt.UTC)


def test_naive_or_malformed_identity_is_invalid() -> None:
    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"A","acquired_at":"2026-08-25T01:02:00"}'
    )
    assert pause_owner.read().status == "invalid"


def test_begin_maintenance_records_and_refresh_re_stamps_the_shepherd() -> None:
    """Task #3270: the hold carries the operator-side shepherd; progress writes
    keep it, a refresh (a new ladder step) re-stamps it."""
    from dataclasses import replace

    from base.deploy.maintenance.hold_driver import HoldDriver, mint_driver

    shepherd = mint_driver()
    before = pause_owner.begin_maintenance("op1", _when(), driver=shepherd).snapshot
    assert before.driver == shepherd
    hold = before.maintenance
    assert hold is not None
    # A progress write (agent-host side) must not claim the ladder.
    stamp = HoldDriver()
    assert pause_owner.refresh_driver("op1", _when(1), driver=stamp) is False  # wrong generation
    assert pause_owner.refresh_driver("op1", _when(), driver=stamp) is True
    after = pause_owner.change_maintenance("op1", _when(), hold, replace(hold, phase="draining"))
    assert after.driver == stamp


def test_a_legacy_journal_without_a_shepherd_reads_none() -> None:
    """A pre-#3270 journal is valid state with missing evidence, not an error."""
    from base.deploy.maintenance.state import MaintenanceHold

    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"A","acquired_at":"2026-08-25T01:02:00+00:00",'
        f'"maintenance":{__import__("json").dumps(MaintenanceHold().encode())}}}'
    )
    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.driver is None


def test_a_malformed_shepherd_degrades_to_none_never_a_broken_journal() -> None:
    """The hold must stay readable when only its shepherd evidence is unusable:
    the verdict reports the missing identity loudly instead."""
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    pause_owner.state_path().write_text(
        json.dumps(
            {
                "state": "paused",
                "holder": "A",
                "acquired_at": "2026-08-25T01:02:00+00:00",
                "maintenance": MaintenanceHold().encode(),
                "driver": {"root": {"pid": -1, "birth": "nope"}},
            }
        )
    )
    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.driver is None


def test_a_journal_written_with_the_retired_reaped_map_still_reads() -> None:
    """A fleet update stops on the old code and starts on the new one, so the new
    code reads the journal the old code left: its retired `reaped` map is ignored
    and never written back."""
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    hold = MaintenanceHold("stopped", {7: 100}, drained=(7,))
    pause_owner.state_path().write_text(
        json.dumps(
            {
                "state": "paused",
                "holder": "A",
                "acquired_at": "2026-08-25T01:02:00+00:00",
                "maintenance": {**hold.encode(), "reaped": {}},
            }
        )
    )
    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.maintenance == hold
    assert "reaped" not in hold.encode()
