from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shared import maintenance, pause_owner
from shared.maintenance_state import MaintenanceHold

WHEN = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def test_maintenance_survives_old_timestamp_and_every_ordinary_resume() -> None:
    pause_owner.begin_maintenance("migration", WHEN - timedelta(days=10))
    assert maintenance.held()
    assert not pause_owner.finalize_natural_resume()
    assert not pause_owner.mark_resumed("migration", WHEN - timedelta(days=10))
    assert not pause_owner.clear("migration", WHEN - timedelta(days=10))
    with pytest.raises(RuntimeError, match="explicit resume"):
        pause_owner.force_clear()
    with pytest.raises(RuntimeError, match="explicit resume"):
        pause_owner.mark_paused("new-rollout", WHEN)
    assert maintenance.held()


def test_normal_start_cannot_release_maintenance() -> None:
    pause_owner.begin_maintenance("migration", WHEN)
    with pytest.raises(RuntimeError, match="cannot release"):
        maintenance.require_start_allowed()


def test_receipts_cannot_substitute_for_a_different_restart_or_generation() -> None:
    first = pause_owner.begin_maintenance("migration", WHEN)
    assert first.maintenance is not None
    hold = MaintenanceHold("draining", {42: 100, 43: 101})
    pause_owner.change_maintenance("migration", WHEN, first.maintenance, hold)
    maintenance.record_drained(42, 100)
    assert maintenance.pending_command(42) is None
    assert maintenance.pending_command(43) == 101
    with pytest.raises(RuntimeError, match="cohort"):
        maintenance.record_drained(43, 102)
    with pytest.raises(RuntimeError, match="fully drained"):
        maintenance.set_phase("migration", WHEN, "drained")
    with pytest.raises(RuntimeError, match="generation"):
        maintenance.require_operation("migration", WHEN + timedelta(seconds=1))
    maintenance.record_drained(43, 101)
    done = maintenance.set_phase("migration", WHEN, "drained")
    assert done.maintenance == replace(hold, phase="drained", drained=(42, 43))
    assert maintenance.held()


def test_malformed_maintenance_never_becomes_an_inactive_deploy_pause() -> None:
    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"migration","acquired_at":"2026-09-06T00:00:00Z",'
        '"maintenance":{"phase":"typo","commands":{},"drained":[]}}'
    )
    assert pause_owner.read().status == "invalid"
    with pytest.raises(RuntimeError, match="unreadable"):
        maintenance.snapshot()


def test_replayed_cohort_write_cannot_drop_a_drain_receipt() -> None:
    original = pause_owner.begin_maintenance("migration", WHEN)
    assert original.maintenance is not None
    hold = MaintenanceHold("draining", {42: 100})
    pause_owner.change_maintenance("migration", WHEN, original.maintenance, hold)
    maintenance.record_drained(42, 100)
    with pytest.raises(RuntimeError, match="progress changed"):
        pause_owner.change_maintenance("migration", WHEN, hold, replace(hold, phase="drained"))
