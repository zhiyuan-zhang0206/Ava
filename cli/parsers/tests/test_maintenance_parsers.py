"""The maintenance commands as the real parser exposes them, and the `status` shepherd printout."""

import json
from unittest.mock import MagicMock

import pytest

from base.deploy.maintenance import hold_driver, pause_owner
from cli.commands.lifecycle import maintenance as command
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


def test_maintenance_exposes_only_the_hold_exits() -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    for verb in ("prepare", "drain", "stop", "start", "resume", "stop-data-plane"):
        with pytest.raises(SystemExit):
            parser.parse_args(["maintenance", verb])


def test_real_parser_dispatches_cancel_to_the_named_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.parsers import build_parser

    action = MagicMock()
    monkeypatch.setattr(command, "cancel", action)
    args = build_parser().parse_args(
        ["maintenance", "cancel", "--operation", "local", "--acquired-at", WHEN.isoformat()]
    )
    assert args.maintenance_cmd == "cancel"
    assert command.run(args) == 0
    action.assert_called_once_with("local", WHEN)


def test_real_parser_exposes_repair_with_operator() -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    parsed = parser.parse_args(
        [
            "maintenance",
            "repair",
            "--operation",
            "local",
            "--acquired-at",
            WHEN.isoformat(),
            "--operator",
            "Ava #5870",
        ]
    )
    assert parsed.maintenance_cmd == "repair"
    assert parsed.operation == "local"
    assert parsed.operator == "Ava #5870"


def test_status_prints_recorded_shepherd_with_liveness(
    capsys: pytest.CaptureFixture[str],
    isolate: None,
) -> None:
    """Task #3276: `maintenance status` surfaces the recorded shepherd.

    The driver block carries the binding process (pid + argv), its session
    leader, and the judged liveness -- all read from local process state, so a
    stuck hold stays inspectable exactly when the gateway is down.
    """
    from cli.parsers import build_parser

    driver = hold_driver.mint_driver()
    pause_owner.begin_maintenance("local", WHEN, driver=driver)
    args = build_parser().parse_args(["maintenance", "status"])
    assert command.run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["driver"]
    assert block is not None
    assert block["liveness"] == "alive"
    assert block["root"]["pid"] > 0
    assert block["root"]["argv"]
    assert "leader" in block


def test_status_keeps_dead_shepherd_visible_as_evidence(
    capsys: pytest.CaptureFixture[str],
    isolate: None,
) -> None:
    """A dead binding stays visible: pid/argv retained, liveness verdict `dead`."""
    from cli.parsers import build_parser

    ghost = hold_driver.ProcessRef(pid=99999, birth=0.0, starttime=1, argv="ghost")
    pause_owner.begin_maintenance("local", WHEN, driver=hold_driver.HoldDriver(root=ghost))
    args = build_parser().parse_args(["maintenance", "status"])
    assert command.run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["driver"]
    assert block["liveness"] == "dead"
    assert block["root"] == {"pid": 99999, "argv": "ghost"}
    assert block["leader"] is None


def test_status_without_recorded_shepherd_prints_null_driver(
    capsys: pytest.CaptureFixture[str],
    isolate: None,
) -> None:
    """No identity recorded (legacy journal / daemon pause) reads as null, not a guess."""
    from cli.parsers import build_parser

    pause_owner.begin_maintenance("local", WHEN)
    args = build_parser().parse_args(["maintenance", "status"])
    assert command.run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["driver"] is None


def test_operation_and_acquired_at_are_gated_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(
            ["maintenance", "cancel", "--operation", "op", "--acquired-at", "soon"]
        )
    assert raised.value.code == 2
    assert "argument --acquired-at:" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(
            ["maintenance", "cancel", "--operation", "op", "--acquired-at", "2026-09-20 03:00:00"]
        )
    assert raised.value.code == 2
    assert "UTC offset" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(
            ["maintenance", "cancel", "--operation", "  ", "--acquired-at", WHEN.isoformat()]
        )
    assert raised.value.code == 2
    assert "argument --operation:" in capsys.readouterr().err
