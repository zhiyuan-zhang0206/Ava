"""`ava mcp enable/disable` write the per-machine MCP-enable overlay, and
`ava mcp list` still shows defined-but-disabled servers with a marker.
`unit_home` isolates ~/.ava per test."""

import json
from pathlib import Path

import pytest

from cli.commands.extensions.mcp import cmd_mcp_add, cmd_mcp_disable, cmd_mcp_enable, cmd_mcp_list


def test_enable_then_disable_writes_overlay(unit_home: Path) -> None:
    from base.packages.plugins.mcp_enabled import read_enabled

    assert cmd_mcp_enable("foo") == 0
    assert read_enabled() == {"foo": True}

    assert cmd_mcp_disable("foo") == 0
    assert read_enabled() == {"foo": False}


def test_disable_filters_from_default_load(unit_home: Path) -> None:
    from ava.mcp_config import load_mcp_config

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
    from base.packages.plugins.mcp_enabled import local_config_path

    cmd_mcp_disable("foo")
    data = json.loads(local_config_path().read_text())
    assert data["mcp_servers"]["foo"]["enabled"] is False


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_enable_rejects_invalid_requirements_without_writing(
    unit_home: Path, requires: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from base.packages.plugins.mcp_enabled import local_config_path

    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], []) == 0
    assert cmd_mcp_disable("foo") == 0
    before = local_config_path().read_bytes()
    capsys.readouterr()

    assert cmd_mcp_enable("foo") == 1
    output = capsys.readouterr()
    assert "requires" in output.err
    assert output.out == ""
    assert local_config_path().read_bytes() == before


def test_disable_preserves_invalid_requirements_as_a_close_operation(unit_home: Path) -> None:
    from base.packages.plugins.mcp_enabled import read_enabled

    spec = {"command": "server-foo", "requires": {"gpu": "invalid"}}
    assert cmd_mcp_add("foo", json.dumps(spec), None, [], []) == 0
    assert cmd_mcp_disable("foo") == 0
    assert read_enabled() == {"foo": False}
    assert json.loads((unit_home / "mcp.json").read_text())["mcpServers"]["foo"] == spec


@pytest.mark.parametrize("requires", [None, {}, {"display": True, "unix_socket": True}])
def test_enable_and_list_validate_declarations_without_host_probes(
    unit_home: Path, requires: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ava.mcp_config as cfg_mod
    from base.packages.plugins.mcp_enabled import read_enabled

    def unexpected_probe() -> bool:
        pytest.fail("CLI declaration validation must not probe host capabilities")

    monkeypatch.setattr(cfg_mod, "display_available", unexpected_probe)
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", unexpected_probe)
    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], []) == 0
    assert cmd_mcp_enable("foo") == 0
    assert read_enabled() == {"foo": True}
    assert cmd_mcp_list() == 0


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_list_rejects_invalid_requirements_before_printing_inventory(
    unit_home: Path, requires: object, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], []) == 0
    assert cmd_mcp_disable("foo") == 0
    capsys.readouterr()

    assert cmd_mcp_list() == 1
    output = capsys.readouterr()
    assert "requires" in output.err
    assert output.out == ""
