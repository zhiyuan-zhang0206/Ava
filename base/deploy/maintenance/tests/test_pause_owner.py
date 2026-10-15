from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenancePhase


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


def test_begin_maintenance_writes_exactly_the_hold_record() -> None:
    pause_owner.begin_maintenance("op1", _when())
    raw = json.loads(pause_owner.state_path().read_text())
    assert set(raw) == {"state", "holder", "acquired_at", "maintenance"}


def test_a_journal_with_the_retired_driver_key_reads_and_loses_it_on_rewrite() -> None:
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    hold = MaintenanceHold(MaintenancePhase.DRAINING, {7: 100})
    legacy_ref = {"pid": 4242, "birth": 1.5, "starttime": 77, "argv": "ava stop"}
    pause_owner.state_path().write_text(
        json.dumps(
            {
                "state": "paused",
                "holder": "A",
                "acquired_at": "2026-08-25T01:02:00+00:00",
                "maintenance": hold.encode(),
                "driver": {"root": legacy_ref, "leader": legacy_ref},
            }
        )
    )
    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.holder == "A"
    assert snapshot.acquired_at == _when()
    assert snapshot.maintenance == hold

    pause_owner.change_maintenance(
        "A", _when(), hold, MaintenanceHold(MaintenancePhase.DRAINED, {7: 100})
    )

    raw = json.loads(pause_owner.state_path().read_text())
    assert "driver" not in raw
    assert pause_owner.read().maintenance == MaintenanceHold(MaintenancePhase.DRAINED, {7: 100})


def test_a_malformed_retired_driver_key_never_breaks_the_journal() -> None:
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
    assert pause_owner.read().status == "paused"


def test_a_journal_written_with_the_retired_reaped_map_still_reads() -> None:
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    hold = MaintenanceHold(MaintenancePhase.STOPPED, {7: 100}, drained=(7,))
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


def test_journal_written_with_the_retired_repair_record_still_decodes() -> None:
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    hold = MaintenanceHold(MaintenancePhase.STOPPED, {7: 100}, drained=(7,))
    pause_owner.state_path().write_text(
        json.dumps(
            {
                "state": "paused",
                "holder": "A",
                "acquired_at": "2026-08-25T01:02:00+00:00",
                "maintenance": {
                    **hold.encode(),
                    "repaired": {"7": "RuntimeError"},
                    "repair_record": {"at": "2026-08-25T01:03:00+00:00", "by": "Ava #1"},
                },
            }
        )
    )
    snapshot = pause_owner.read()
    assert snapshot.maintenance == hold
    assert "repaired" not in hold.encode()
    assert "repair_record" not in hold.encode()


@pytest.mark.parametrize("phase", ["preparing", "draining", "drained", "stopping", "stopped"])
def test_maintenance_journal_restores_owned_phase_vocabulary(phase: str) -> None:
    import json

    from base.deploy.maintenance.state import MaintenanceHold

    wire = MaintenanceHold(commands={7: 100}, drained=(7,), parked=(8,)).encode()
    wire["phase"] = phase
    restored = MaintenanceHold.decode(json.loads(json.dumps(wire)))
    assert restored.phase is MaintenancePhase(phase)
    assert restored.encode() == wire


@pytest.mark.parametrize("phase", ["future", "starting", "ready", None, 7, [], {}])
def test_maintenance_journal_rejects_unknown_phase(phase: object) -> None:
    from base.deploy.maintenance.state import MaintenanceHold

    with pytest.raises(ValueError, match="maintenance phase"):
        MaintenanceHold.decode({**MaintenanceHold().encode(), "phase": phase})
