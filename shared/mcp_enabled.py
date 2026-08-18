"""MCP enable overlay — per-machine local file `~/.ava/mcp_enabled.json`.

Different concept from the MCP server definitions themselves (which the
loader merges from builtin `ava_builtins/mcps/*/.mcp.json` < plugin `.mcp.json` <
machine `mcp.json`): this overlay only records, per host, which already-defined
servers are disabled. Symmetric with how plugins are toggled via
`~/.ava/plugins_config.json`; editable via `ava mcp enable/disable`.

On-disk shape — a flat `mcp_servers` dict: {name: {enabled: bool}}.
A server NOT present in the map defaults to enabled (default-on); the
default is applied by the consumer (the MCP config loader), so `read_enabled`
returns only the explicit entries.

Decentralized-install: the overlay lives entirely in the per-machine
`mcp_enabled.json`; `set_mcp_enabled` is the only writer. Reads go through
`read_enabled` and are pure-read (no write-back). `write_local` is the
underlying file writer.
"""

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from shared import paths

_log = logging.getLogger(__name__)


class McpServerEntry(BaseModel):
    """Enable state of a single MCP server."""

    enabled: bool


class McpEnabledConfig(BaseModel):
    """MCP enable overlay — flat dict of explicit per-server enable states."""

    mcp_servers: dict[str, McpServerEntry] = {}


class McpEnabledConfigError(Exception):
    """Root of mcp_enabled.json read or validation failures."""


class SchemaInvalid(McpEnabledConfigError):  # noqa: N818
    """JSON is valid but does not match the McpEnabledConfig schema (Pydantic validation failed)."""


def _validate_schema(data: dict[str, Any]) -> McpEnabledConfig:
    try:
        return McpEnabledConfig.model_validate(data)
    except ValidationError as e:
        raise SchemaInvalid(f"mcp_enabled.json schema invalid: {e}") from e


def local_config_path() -> Path:
    """Per-machine MCP-enable overlay file path: `~/.ava/mcp_enabled.json`."""
    return paths.ava_home() / "mcp_enabled.json"


class _MalformedOverlayError(McpEnabledConfigError):
    """The overlay file exists but cannot be parsed — the operator's explicit
    enable/disable intent is unknown, and defaulting it to "all enabled"
    would silently resurrect servers the operator disabled (audit 2026-08-08
    P2: the fail-open default turned a corrupt file into a security-relevant
    state change with no visible error)."""


def _read_local() -> dict[str, Any] | None:
    """Read the per-machine MCP-enable overlay file. None = absent; a
    present-but-malformed file raises `_MalformedOverlay` — the caller decides
    the fail-closed behavior instead of inheriting fail-open."""
    p = local_config_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text() or "{}")
    except (OSError, json.JSONDecodeError) as e:
        _log.error(
            "mcp_enabled: local file %s is malformed — treating every MCP server as disabled", p
        )
        raise _MalformedOverlayError(f"mcp_enabled.json unreadable: {e}") from e
    return dict(cast("dict[str, Any]", data)) if isinstance(data, dict) else {}


def write_local(cfg: McpEnabledConfig) -> None:
    """Full-replace the per-machine MCP-enable overlay file. The write path for
    `set_mcp_enabled` (and `ava mcp enable/disable`). Atomic (temp + replace):
    a crash mid-write must not produce the malformed file that fail-closed
    reads treat as all-disabled (audit 2026-08-08 P2)."""
    p = local_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg.model_dump(), f, indent=2)
        Path(tmp).replace(p)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise


def read_enabled() -> dict[str, bool]:
    """Read the per-machine overlay; return {server-name: enabled} for the explicit
    entries only. Absent file -> {}. A present-but-malformed file RAISES
    `McpEnabledConfigError` (fail-closed): a corrupt overlay's intent is
    unknown, and the caller must not inherit "everything enabled" by default
    (audit 2026-08-08 P2). Consumers catch it and disable every server.

    A server NOT present in the returned map means "enabled" (default-on); that
    default is applied by the consumer, not here.
    """
    raw = _read_local()
    if not raw or not raw.get("mcp_servers"):
        return {}
    cfg = _validate_schema(raw)
    return {name: entry.enabled for name, entry in cfg.mcp_servers.items()}


def set_mcp_enabled(name: str, *, enabled: bool) -> McpEnabledConfig:
    """Set one MCP server's enabled flag in the per-machine local overlay file.

    Reads the current overlay, sets `mcp_servers[name] = {enabled}`, full-replaces
    the file, and returns the config. No "is this server defined?" check — an
    overlay entry for a server that isn't currently defined is harmless, it just
    filters nothing.
    """
    raw = _read_local() or {}
    existing = _validate_schema(raw).mcp_servers if raw.get("mcp_servers") else {}
    servers = dict(existing)
    servers[name] = McpServerEntry(enabled=enabled)
    cfg = McpEnabledConfig(mcp_servers=servers)
    write_local(cfg)
    return cfg
