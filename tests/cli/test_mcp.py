"""`ava mcp add/list/remove` — edit the machine MCP config at $AVA_HOME/mcp.json.
`unit_home` isolates ~/.ava per test."""

import argparse
import json
from pathlib import Path

import pytest

from cli.commands.mcp import cmd_mcp_add, cmd_mcp_list, cmd_mcp_remove


def _config(home: Path) -> dict:
    return json.loads((home / "mcp.json").read_text())


def test_add_from_json_spec(unit_home: Path) -> None:
    spec = '{"command": "npx", "args": ["-y", "server-foo"], "env": {"K": "v"}}'
    assert cmd_mcp_add("foo", spec, None, [], []) == 0
    servers = _config(unit_home)["mcpServers"]
    assert servers["foo"] == {"command": "npx", "args": ["-y", "server-foo"], "env": {"K": "v"}}


def test_add_from_command_flags(unit_home: Path) -> None:
    assert cmd_mcp_add("bar", None, "uvx", ["server-bar"], ["TOKEN=abc"]) == 0
    assert _config(unit_home)["mcpServers"]["bar"] == {
        "command": "uvx",
        "args": ["server-bar"],
        "env": {"TOKEN": "abc"},
    }


def test_add_merges_without_clobbering(unit_home: Path) -> None:
    cmd_mcp_add("a", None, "cmd-a", [], [])
    cmd_mcp_add("b", None, "cmd-b", [], [])
    servers = _config(unit_home)["mcpServers"]
    assert set(servers) == {"a", "b"}  # pyright: ignore[reportUnknownArgumentType]


def test_add_replace_reports(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    cmd_mcp_add("a", None, "cmd-a", [], [])
    assert cmd_mcp_add("a", None, "cmd-a2", [], []) == 0
    assert "replaced" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _config(unit_home)["mcpServers"]["a"]["command"] == "cmd-a2"


def test_add_rejects_both_forms(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_add("x", '{"command": "c"}', "c", [], []) == 1
    assert "not both" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert not (unit_home / "mcp.json").exists()


def test_add_rejects_neither_form(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_add("x", None, None, [], []) == 1
    assert "need --json" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_add_rejects_bad_json(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_add("x", "{not json}", None, [], []) == 1
    assert not (unit_home / "mcp.json").exists()


def test_add_rejects_non_object_json(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_add("x", "[1, 2]", None, [], []) == 1
    assert "server object" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_add_rejects_bad_env_pair(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_add("x", None, "cmd", [], ["NOEQUALS"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ── parse-layer discipline: a malformed invocation never reaches command code ─


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


def test_list_without_machine_config_flags_builtins_readonly(
    unit_home: Path, capsys: pytest.CaptureFixture
) -> None:
    # no machine config yet → only built-in / plugin-bundled servers, all read-only here
    assert cmd_mcp_list() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "machine" not in out
    assert "read-only here" in out


def test_list_shows_machine_entry(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    cmd_mcp_add("foo", None, "npx", ["-y", "server-foo"], [])
    assert cmd_mcp_list() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "foo" in out and "machine" in out
    assert "npx -y server-foo" in out


def test_remove_machine_entry(unit_home: Path) -> None:
    cmd_mcp_add("foo", None, "npx", [], [])
    assert cmd_mcp_remove("foo") == 0
    assert _config(unit_home).get("mcpServers", {}) == {}  # pyright: ignore[reportUnknownMemberType]


def test_remove_unknown_errors(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_remove("nope") == 1
    assert "not a machine-config server" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
