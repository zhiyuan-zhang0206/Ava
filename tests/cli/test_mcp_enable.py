"""`ava mcp enable/disable` write the per-machine MCP-enable overlay, and
`ava mcp list` still shows defined-but-disabled servers with a marker.
`unit_home` isolates ~/.ava per test."""

import json
from pathlib import Path

import pytest

from cli.commands import cmd_mcp_add, cmd_mcp_disable, cmd_mcp_enable, cmd_mcp_list


def test_enable_then_disable_writes_overlay(unit_home: Path) -> None:
    from shared.mcp_enabled import read_enabled

    assert cmd_mcp_enable("foo") == 0
    assert read_enabled() == {"foo": True}

    assert cmd_mcp_disable("foo") == 0
    assert read_enabled() == {"foo": False}


def test_disable_filters_from_default_load(unit_home: Path) -> None:
    from ava._mcp_config import load_mcp_config

    cmd_mcp_add("foo", None, "npx", ["-y", "server-foo"], [])
    assert "foo" in load_mcp_config()

    assert cmd_mcp_disable("foo") == 0
    # default (filtering) load no longer returns it; include_disabled still does
    assert "foo" not in load_mcp_config()
    assert "foo" in load_mcp_config(include_disabled=True)


def test_list_marks_disabled_server(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    cmd_mcp_add("foo", None, "npx", ["-y", "server-foo"], [])
    cmd_mcp_disable("foo")
    capsys.readouterr()  # drop add/disable output

    assert cmd_mcp_list() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "foo" in out
    assert "[disabled]" in out


def test_disable_hints_when_undefined(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_disable("nope") == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "no server named 'nope' is currently defined" in out


def test_overlay_file_shape(unit_home: Path) -> None:
    from shared.mcp_enabled import local_config_path

    cmd_mcp_disable("foo")
    data = json.loads(local_config_path().read_text())
    assert data["mcp_servers"]["foo"]["enabled"] is False
