"""The `ava schedules` argument surface."""

from __future__ import annotations

import pytest

from cli.main import _build_parser


def test_every_schedules_verb_is_registered() -> None:
    """The parser exposes every schedules verb — the routed ones plus the
    local provision / verify pair."""
    import argparse
    from typing import cast

    p = _build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    schedules_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["schedules"]
    sub = next(a for a in schedules_p._actions if a.dest == "schedules_cmd")
    assert set(cast("dict[str, object]", sub.choices)) == {
        "ls",
        "get",
        "create",
        "update",
        "delete",
        "provision",
        "verify",
        "start",
        "stop",
        "restart",
        "logs",
        "runs",
    }


def test_verify_flags_parse() -> None:
    args = _build_parser().parse_args(["schedules", "verify"])
    assert args.check_file is None and args.no_notify is False
    args = _build_parser().parse_args(
        ["schedules", "verify", "--check-file", "x.py", "--no-notify"]
    )
    assert args.check_file == "x.py" and args.no_notify is True


def test_script_flags_are_mutually_exclusive() -> None:
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["schedules", "create", "--name", "n", "--script", "x", "--script-file", "f"])


def test_enable_and_disable_are_mutually_exclusive() -> None:
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["schedules", "update", "7", "--enable", "--disable"])


def test_create_requires_a_script_source_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither source is refused by the parse layer (previously the command body)."""
    p = _build_parser()
    with pytest.raises(SystemExit) as raised:
        p.parse_args(["schedules", "create", "--name", "n"])
    assert raised.value.code == 2
    assert "one of the arguments --script --script-file is required" in capsys.readouterr().err


def test_update_requires_at_least_one_field_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _build_parser().parse_args(["schedules", "update", "7"])
    assert args.func(args) == 2
    assert "at least one" in capsys.readouterr().err


def test_update_passes_the_parse_gate_with_one_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands.management import schedules as _schedules

    def fake(_args: object) -> int:
        return 0

    monkeypatch.setattr(_schedules, "h_schedules_update", fake)
    args = _build_parser().parse_args(["schedules", "update", "7", "--enable"])
    assert args.func(args) == 0
