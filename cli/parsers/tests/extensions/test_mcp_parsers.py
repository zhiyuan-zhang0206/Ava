"""`mcp add` validates its spec source, JSON and environment at parse time."""

import argparse

import pytest


def _add_args(*extra: str) -> argparse.Namespace:
    from cli.parsers import build_parser

    return build_parser().parse_args(["mcp", "add", "x", *extra])


def test_add_requires_exactly_one_spec_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _add_args()
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "--json" in err and "--command" in err

    with pytest.raises(SystemExit) as raised:
        _add_args("--json", '{"command": "c"}', "--command", "c")
    assert raised.value.code == 2


def test_add_rejects_bad_spec_json_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _add_args("--json", "{not json}")
    assert raised.value.code == 2
    assert "argument --json:" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _add_args("--json", "[1, 2]")
    assert raised.value.code == 2
    assert "must be a JSON object" in capsys.readouterr().err


def test_add_rejects_bad_env_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _add_args("--command", "c", "--env", "NOEQUALS")
    assert raised.value.code == 2
    assert "argument --env:" in capsys.readouterr().err


def test_add_arg_or_env_without_command_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _add_args("--json", '{"command": "c"}', "--env", "K=V")
    assert args.func(args) == 2
    assert "--command" in capsys.readouterr().err


def test_add_parse_accepts_both_legal_forms() -> None:
    assert _add_args("--json", '{"command": "c"}').json == '{"command": "c"}'
    args = _add_args("--command", "c", "--arg", "server-bar", "--arg=-y", "--env", "K=V")
    assert args.command == "c" and args.arg == ["server-bar", "-y"] and args.env == ["K=V"]
