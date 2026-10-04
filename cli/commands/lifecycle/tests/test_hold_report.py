"""`ava status` reports the maintenance hold: a human section, and `--json` for machine readers."""

import json

import pytest

from base.deploy.maintenance import hold_driver, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from cli.commands.lifecycle import hold_report
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


def _json_hold(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    from cli.parsers import build_parser

    args = build_parser().parse_args(["status", "--json"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # one object, nothing else on stdout
    return json.loads(out)["hold"]


def test_status_json_of_a_free_unit(capsys: pytest.CaptureFixture[str], isolate: None) -> None:
    hold = _json_hold(capsys)
    assert hold["status"] == "inactive"
    assert hold["maintenance"] is None
    assert hold["driver"] is None


def test_status_json_carries_the_generation_phase_and_failures(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    held = MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, held)

    hold = _json_hold(capsys)

    assert hold["status"] == "paused"
    assert hold["operation"] == "local"
    assert hold["acquired_at"] == WHEN.isoformat()
    assert hold["maintenance"] == held.encode()


def test_status_json_surfaces_recorded_shepherd_with_liveness(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    """The driver block carries the binding process (pid + argv), its session leader and the
    judged liveness, all read from local process state: a stuck hold stays inspectable exactly
    when the gateway is down."""
    pause_owner.begin_maintenance("local", WHEN, driver=hold_driver.mint_driver())

    block = _json_hold(capsys)["driver"]

    assert isinstance(block, dict)
    assert block["liveness"] == "alive"
    assert block["root"]["pid"] > 0
    assert block["root"]["argv"]
    assert "leader" in block


def test_status_json_keeps_a_dead_shepherd_visible(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    ghost = hold_driver.ProcessRef(pid=99999, birth=0.0, starttime=1, argv="ghost")
    pause_owner.begin_maintenance("local", WHEN, driver=hold_driver.HoldDriver(root=ghost))

    block = _json_hold(capsys)["driver"]

    assert isinstance(block, dict)
    assert block["liveness"] == "dead"
    assert block["root"] == {"pid": 99999, "argv": "ghost"}
    assert block["leader"] is None


def test_human_section_names_the_phase_and_the_exit(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    hold_report.print_hold_section()
    assert "maintenance hold: none" in capsys.readouterr().out

    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    held = MaintenanceHold("draining", {7: 70}, failures={7: "RuntimeError"})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, held)

    hold_report.print_hold_section()
    out = capsys.readouterr().out
    assert "maintenance hold: draining" in out
    assert "operation=local" in out
    assert "failures=[7]" in out
    assert "`ava start`" in out


def test_human_section_reports_an_unreadable_journal(
    capsys: pytest.CaptureFixture[str], isolate: None
) -> None:
    pause_owner.state_path().write_text("{not json")
    hold_report.print_hold_section()
    out = capsys.readouterr().out
    assert "unreadable journal" in out
