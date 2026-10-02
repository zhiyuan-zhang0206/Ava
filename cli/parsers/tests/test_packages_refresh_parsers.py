"""`ava packages policy` requires a field, rejects a bad duration at parse time and accepts explicit fields."""

from __future__ import annotations

import pytest


def test_policy_cli_requires_at_least_one_field(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    args = build_parser().parse_args(["packages", "policy", "foo"])
    assert args.func(args) == 2
    assert "--update-mode" in capsys.readouterr().err


def test_policy_cli_rejects_bad_duration_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["packages", "policy", "foo", "--check-every", "soon"])
    assert raised.value.code == 2
    assert "argument --check-every:" in capsys.readouterr().err


def test_policy_cli_accepts_explicit_fields() -> None:
    from cli.parsers import build_parser

    args = build_parser().parse_args(["packages", "policy", "foo", "--check-every", "2h"])
    assert args.check_every == "2h" and args.update_mode is None
