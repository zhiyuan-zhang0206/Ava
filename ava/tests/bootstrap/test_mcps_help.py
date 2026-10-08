"""`ava.help(ava.mcps)` renders the MCP server index and nothing more."""

import json
from pathlib import Path

import pytest

import ava


@pytest.fixture
def fake_config(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ava_home() to tmpdir; the repo's built-in .mcp.json files are not listed."""
    import ava.mcp_config as _cfg

    monkeypatch.setattr(_cfg, "builtin_mcp_paths", list)
    return unit_home / "mcp.json"


def test_help_on_mcps_module_is_index_only(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava.help(ava.mcps)` is an INDEX: each configured server as a name plus at
    most its one-liner, never its tool list or a tool's JSON Schema. Pinning it
    because the `# Capabilities` section is the prompt's single MCP index — a
    render that reached for tools would connect to every configured server and
    put every tool schema in front of the agent. Tools stay one
    `ava.help(ava.mcps.<server>)` away."""
    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "x", "description": "Local filesystem"},
                    "github": {"command": "y"},
                }
            }
        ),
        encoding="utf-8",
    )
    ava.help(ava.mcps)
    out = capsys.readouterr().out
    assert "from . import fs" in out
    assert "from . import github" in out
    # Positively: each server carries its one-liner. It is the proxy's generic
    # doc, not the `description` from mcp.json — that one reaches the agent
    # through the `# Capabilities` index, which is the MCP index of record.
    assert "Tools of MCP server 'fs'." in out
    # No tool-schema surface: the render must not have connected to a server.
    assert "inputSchema" not in out
    assert "**kwargs" not in out
