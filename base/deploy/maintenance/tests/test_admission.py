import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold

WHEN = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def test_maintenance_hold_has_no_expiry() -> None:
    pause_owner.begin_maintenance("migration", WHEN - timedelta(days=10))
    assert admission.held()


def test_normal_start_cannot_release_maintenance() -> None:
    pause_owner.begin_maintenance("migration", WHEN)
    with pytest.raises(RuntimeError, match="cannot release"):
        admission.require_start_allowed()


def test_receipts_cannot_substitute_for_a_different_restart_or_generation() -> None:
    first = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert first.maintenance is not None
    hold = MaintenanceHold("draining", {42: 100, 43: 101})
    pause_owner.change_maintenance("migration", WHEN, first.maintenance, hold)
    admission.record_drained(42, 100)
    assert admission.pending_command(42) is None
    assert admission.pending_command(43) == 101
    with pytest.raises(RuntimeError, match="cohort"):
        admission.record_drained(43, 102)
    with pytest.raises(RuntimeError, match="fully drained"):
        admission.set_phase("migration", WHEN, "drained")
    with pytest.raises(RuntimeError, match="generation"):
        admission.require_operation("migration", WHEN + timedelta(seconds=1))
    admission.record_drained(43, 101)
    done = admission.set_phase("migration", WHEN, "drained")
    assert done.maintenance == replace(hold, phase="drained", drained=(42, 43))
    assert admission.held()


def test_malformed_maintenance_never_becomes_an_inactive_deploy_pause() -> None:
    pause_owner.state_path().write_text(
        '{"state":"paused","holder":"migration","acquired_at":"2026-09-06T00:00:00Z",'
        '"maintenance":{"phase":"typo","commands":{},"drained":[]}}'
    )
    assert pause_owner.read().status == "invalid"
    with pytest.raises(RuntimeError, match="unreadable"):
        admission.snapshot()


def test_replayed_cohort_write_cannot_drop_a_drain_receipt() -> None:
    original = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert original.maintenance is not None
    hold = MaintenanceHold("draining", {42: 100})
    pause_owner.change_maintenance("migration", WHEN, original.maintenance, hold)
    admission.record_drained(42, 100)
    with pytest.raises(RuntimeError, match="progress changed"):
        pause_owner.change_maintenance("migration", WHEN, hold, replace(hold, phase="drained"))


def test_quiesced_covers_only_the_stop_window() -> None:
    assert not admission.quiesced()

    first = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert first.maintenance is not None
    assert first.maintenance.phase == "preparing"
    assert not admission.quiesced()

    pause_owner.change_maintenance(
        "migration", WHEN, first.maintenance, MaintenanceHold("draining")
    )
    assert not admission.quiesced()

    for phase in ("drained", "stopping", "stopped", "starting", "ready"):
        current = admission.set_phase("migration", WHEN, phase)
        assert current.maintenance is not None
        assert admission.quiesced(), phase

    final = admission.snapshot()
    assert final is not None and final.maintenance is not None
    pause_owner.change_maintenance(
        "migration", WHEN, final.maintenance, final.maintenance, resumed=True
    )
    assert not admission.quiesced()


def test_in_stop_leg_covers_only_the_drained_to_stopped_slice() -> None:
    assert not admission.in_stop_leg()

    first = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert first.maintenance is not None
    assert not admission.in_stop_leg()

    pause_owner.change_maintenance(
        "migration", WHEN, first.maintenance, MaintenanceHold("draining")
    )
    assert not admission.in_stop_leg()

    for phase in ("drained", "stopping", "stopped"):
        current = admission.set_phase("migration", WHEN, phase)
        assert current.maintenance is not None
        assert admission.in_stop_leg(), phase

    for phase in ("starting", "ready"):
        current = admission.set_phase("migration", WHEN, phase)
        assert current.maintenance is not None
        assert not admission.in_stop_leg(), phase


def test_business_gate_tracks_the_journal_without_posture_or_time() -> None:
    assert not admission.business_paused()

    first = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert first.maintenance is not None
    assert not admission.business_paused()
    pause_owner.change_maintenance(
        "migration", WHEN, first.maintenance, MaintenanceHold("draining")
    )
    assert not admission.business_paused()
    admission.set_phase("migration", WHEN, "drained")
    assert not admission.business_paused()  # Other hosts may still need this gateway's SDK.
    for phase in ("stopping", "stopped", "starting", "ready"):
        admission.set_phase("migration", WHEN, phase)
        assert admission.business_paused(), phase
    final = admission.snapshot()
    assert final is not None and final.maintenance is not None
    pause_owner.change_maintenance(
        "migration", WHEN, final.maintenance, final.maintenance, resumed=True
    )
    assert not admission.business_paused()
    with pytest.raises(RuntimeError, match="already resumed"):
        pause_owner.begin_maintenance("migration", WHEN)


def _retired_updater_record(state: str, holder: str) -> None:
    """The plain record (no maintenance hold) the retired updater's stop op wrote."""
    record = {"state": state, "holder": holder, "acquired_at": WHEN.isoformat()}
    pause_owner.state_path().write_text(json.dumps(record))


def test_business_gate_refuses_incomplete_and_invalid_pause_records() -> None:
    _retired_updater_record("paused", "incomplete")
    assert admission.business_paused()
    pause_owner.state_path().write_text("invalid")
    assert admission.business_paused()


def test_resumed_tombstone_cannot_reopen_without_a_new_generation() -> None:
    _retired_updater_record("resumed", "completed")
    with pytest.raises(RuntimeError, match="already resumed"):
        pause_owner.begin_maintenance("completed", WHEN)
    assert pause_owner.read().status == "resumed"


def test_business_gate_refuses_unreadable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable() -> None:
        raise OSError("unreadable journal")

    monkeypatch.setattr(pause_owner, "read", unreadable)
    assert admission.business_paused()


def test_windows_read_an_unreadable_owner_as_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable() -> None:
        raise OSError("owner state unreadable")

    monkeypatch.setattr(admission, "snapshot", _unreadable)
    assert admission.quiesced()
    assert admission.in_stop_leg()


def test_clear_failures_drops_blocking_receipts_and_keeps_the_rest() -> None:
    first = pause_owner.begin_maintenance("migration", WHEN).snapshot
    assert first.maintenance is not None
    hold = MaintenanceHold(
        "draining",
        {1: 11, 2: 22},
        drained=(2,),
        failures={1: "RuntimeError"},
        undelivered={2: "PoolTimeout"},
    )
    pause_owner.change_maintenance("migration", WHEN, first.maintenance, hold)

    assert admission.clear_failures() == {1: "RuntimeError"}

    current = admission.snapshot()
    assert current is not None
    assert current.maintenance == replace(hold, failures={})
    assert admission.clear_failures() == {}


def test_clear_failures_outside_a_hold_is_a_no_op() -> None:
    assert admission.clear_failures() == {}
