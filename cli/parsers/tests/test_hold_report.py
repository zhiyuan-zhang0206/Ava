import json

import pytest

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.tests.maintenance_factory import WHEN
from base.deploy.tests.maintenance_factory import isolate as isolate
from cli.commands.lifecycle import hold_report


def _json_hold(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    from cli.parsers import build_parser

    args = build_parser().parse_args(["status", "--json"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    return json.loads(out)["hold"]


def test_status_json_of_a_free_unit(capsys: pytest.CaptureFixture[str], isolate: None) -> None:
    hold = _json_hold(capsys)
    assert hold["status"] == "inactive"
    assert hold["maintenance"] is None


_HOLD_KEYS = {"status", "operation", "acquired_at", "maintenance", "scope"}


def test_status_json_hold_has_exactly_the_documented_keys(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    assert set(_json_hold(capsys)) == _HOLD_KEYS
    pause_owner.begin_maintenance("local", WHEN)
    assert set(_json_hold(capsys)) == _HOLD_KEYS


def test_status_json_carries_the_generation_and_phase(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    held = MaintenanceHold(MaintenancePhase.DRAINING, {1: 11})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, held)

    hold = _json_hold(capsys)

    assert hold["status"] == "paused"
    assert hold["operation"] == "local"
    assert hold["acquired_at"] == WHEN.isoformat()
    assert hold["maintenance"] == held.encode()


def test_human_section_names_the_phase_and_the_exit(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    hold_report.print_hold_section()
    assert "maintenance hold: none" in capsys.readouterr().out

    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    held = MaintenanceHold(MaintenancePhase.DRAINING, {7: 70})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, held)

    hold_report.print_hold_section()
    out = capsys.readouterr().out
    assert "maintenance hold: draining" in out
    assert "operation=local" in out
    assert "driver" not in out
    assert "failures" not in out
    assert "`meiva start`" in out


def test_human_section_reports_an_unreadable_journal(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    pause_owner.state_path().write_text("{not json")
    hold_report.print_hold_section()
    out = capsys.readouterr().out
    assert "unreadable journal" in out
