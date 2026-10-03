"""The `ava mcp install` argument surface."""

import pytest


def test_install_rejects_bad_env_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["mcp", "install", "src", "--env", "NOEQUALS"])
    assert raised.value.code == 2
    assert "argument --env:" in capsys.readouterr().err
