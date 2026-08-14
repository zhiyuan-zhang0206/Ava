"""shared/mcp_enabled.py unit tests — per-host MCP enable overlay.

The overlay is per-machine local only: reads and writes go through
~/.ava/mcp_enabled.json. No DB involvement. `unit_home` points `ava_home()`
at a tmp dir.
"""

import json
from pathlib import Path

import pytest

from shared.mcp_enabled import (
    McpEnabledConfig,
    McpEnabledConfigError,
    McpServerEntry,
    SchemaInvalid,
    local_config_path,
    read_enabled,
    set_mcp_enabled,
    write_local,
)

# ── schema ──


def test_mcp_server_entry_field():
    assert McpServerEntry(enabled=True).enabled is True


def test_mcp_enabled_config_defaults():
    assert McpEnabledConfig().mcp_servers == {}


# ── read_enabled ──


def test_read_enabled_empty_when_no_file(unit_home: Path):
    """Absent overlay file -> {} (default-on applied by the consumer)."""
    assert not local_config_path().exists()
    assert read_enabled() == {}


def test_read_enabled_fails_closed_when_malformed(unit_home: Path):
    """A present-but-malformed file RAISES (fail-closed, audit 2026-08-08 P2):
    the operator's explicit enable/disable intent is unknown, and defaulting
    to "all enabled" would silently resurrect servers the operator disabled.
    Consumers catch the error and disable every server."""
    from shared.mcp_enabled import McpEnabledConfigError

    local_config_path().write_text("{not json")
    with pytest.raises(McpEnabledConfigError):
        read_enabled()


def test_write_local_is_atomic_and_leaves_no_tmp(unit_home: Path):
    """write_local is temp+replace: a crash mid-write cannot leave the
    malformed file that fail-closed reads treat as all-disabled (audit
    2026-08-08 P2 — the old bare write_text produced exactly that file on a
    partial write)."""
    cfg = McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=False)})
    write_local(cfg)
    assert read_enabled() == {"fs": False}
    assert list(local_config_path().parent.glob(".*.tmp")) == []


def test_read_enabled_returns_explicit_entries(unit_home: Path):
    write_local(
        McpEnabledConfig(
            mcp_servers={
                "fs": McpServerEntry(enabled=False),
                "github": McpServerEntry(enabled=True),
            }
        )
    )
    assert read_enabled() == {"fs": False, "github": True}


# ── set_mcp_enabled (round-trip) ──


def test_set_mcp_enabled_round_trip(unit_home: Path):
    """Toggling writes the per-machine overlay; read_enabled reflects the flag."""
    cfg = set_mcp_enabled("fs", enabled=False)
    assert cfg.mcp_servers["fs"].enabled is False
    assert read_enabled() == {"fs": False}

    on_disk = json.loads(local_config_path().read_text())
    assert on_disk["mcp_servers"]["fs"]["enabled"] is False


def test_set_mcp_enabled_disabled_reads_back_false(unit_home: Path):
    set_mcp_enabled("fs", enabled=False)
    assert read_enabled()["fs"] is False


def test_set_mcp_enabled_preserves_other_entries(unit_home: Path):
    """Setting one server's flag does not clobber existing entries."""
    set_mcp_enabled("fs", enabled=False)
    set_mcp_enabled("github", enabled=True)
    assert read_enabled() == {"fs": False, "github": True}


def test_set_mcp_enabled_allows_undefined_server(unit_home: Path):
    """No "is this server defined?" check — an overlay entry for an undefined
    server is harmless (it just filters nothing)."""
    cfg = set_mcp_enabled("never-defined", enabled=False)
    assert cfg.mcp_servers["never-defined"].enabled is False


# ── error hierarchy ──


def test_schema_invalid_is_config_error():
    assert issubclass(SchemaInvalid, McpEnabledConfigError)


def test_read_enabled_raises_schema_invalid_on_bad_shape(unit_home: Path):
    """Valid JSON but wrong schema (non-bool enabled) -> SchemaInvalid."""
    local_config_path().write_text(json.dumps({"mcp_servers": {"fs": {"enabled": [1, 2]}}}))
    with pytest.raises(SchemaInvalid):
        read_enabled()
