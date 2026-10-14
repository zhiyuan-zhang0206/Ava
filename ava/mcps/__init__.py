"""Tools exposed by external MCP servers. Call `ava.mcps.<server>.<tool>(...)`
with the tool's named arguments."""

# Package door. This `__init__` IS the agent-facing `ava.mcps` namespace — its
# docstring above is what `ava.help()` shows, so the package notes live in this
# comment. The `_`-private submodules are the MCP plumbing behind it:
#   _remote     client of the per-machine MCP daemon socket (the call path here)
#   _oauth      OAuth 2.1 client + token storage for remote (`url`) servers
#   _daemon     the shared per-machine daemon process
#               (`python -m ava.mcps._daemon`, ops roster session "mcp-daemon")
#   _browser    the daemon's in-process line client for the browser-mcp service
#   _computer   the daemon's in-process line client for the computer-mcp service
# Every submodule is private on purpose: `ava.mcps.<name>` resolves to an MCP
# server proxy, so a public submodule would shadow the server of the same name.
# For the same reason the server configuration (`ava.mcps.config`, imported by
# cli/ and ops/) stays outside this package.

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

from ava.mcps import _tools
from ava.mcps._browser import (
    release_agent_chrome_pages as release_agent_chrome_pages,
)
from ava.mcps._clients import McpClients
from ava.mcps.config import (
    MCPCallError as MCPCallError,
)
from ava.mcps.config import (
    MCPConnectError as MCPConnectError,
)
from ava.mcps.config import (
    MCPError as MCPError,
)
from ava.mcps.config import (
    MCPServerNotFound,
    load_mcp_config,
)
from ava.mcps.config import (
    MCPToolNotFound as MCPToolNotFound,
)
from ava.mcps.config import (
    ToolInfo as ToolInfo,
)
from ava.sdk_surface import agent_identity, process_context
from ava.sdk_surface.validation import coerce_str

# The module whose `call_raw` every tool call funnels through, text form and `raw` form alike.
# The SDK metering wrapper replaces that attribute (`ava/sdk_surface/metering.py`), so it reaches
# the module through this name instead of importing the private `_tools`.
call_funnel = _tools


def _cache_dir() -> Path:
    """Disk cache directory for tool metadata. Same `_config_path` route through ava_home()."""
    from base.paths import ava_home

    return ava_home() / "mcp_cache"


def _validate_identity() -> None:
    """The external-identity check made before a tool call."""
    agent_identity.validate_external_identity()


def _source() -> _tools.ToolSource:
    """What this process's tools run against: the bound context's daemon client (built on first
    use; the SDK never starts servers), its live timeout and the disk cache."""
    clients = process_context.current_context().clients.get(McpClients)
    return _tools.ToolSource(
        remote=clients.remote,
        timeout_seconds=clients.timeout_seconds,
        cache=_tools.DiskCache(_cache_dir),
        validate_identity=_validate_identity,
    )


def load_config() -> dict[str, dict[str, Any]]:
    """Merged MCP server map (machine `$AVA_HOME/mcp.json` + plugin-bundled `.mcp.json`).

    Returns an empty dict when nothing is configured.

    Raises:
        MCPError: a source file fails to parse / has a non-dict `mcpServers`.
    """
    return load_mcp_config()


def servers() -> list[str]:
    return sorted(load_config().keys())


def description(server: str) -> str | None:
    """One-line summary of a configured server, or None when it declares
    none."""
    server = coerce_str(server, "server")
    config = load_config()
    if server not in config:
        raise MCPServerNotFound(f"ava.mcps has no server {server!r} (have: {sorted(config)})")
    return config[server].get("description")


def __getattr__(name: str) -> Any:
    """`ava.mcps.<server>` lazily returns server proxy (module-level PEP 562)."""
    if name.startswith("_"):
        raise AttributeError(name)
    if name not in load_config():
        raise AttributeError(
            f"ava.mcps has no server {name!r} (not configured in `$AVA_HOME/mcp.json`; have: {servers()})"
        )
    return _tools.ServerProxy(name, _source())


def __dir__() -> list[str]:
    """List every configured server name plus the module's public helpers."""
    return [*servers(), "servers", "description"]


class _McpsModule(types.ModuleType):
    """The class of the `ava.mcps` module object itself.

    Carries `__all_for_ava__` as a property, like `ava.skills`: the agent-visible members are
    the two helpers below plus every configured server (`ava.mcps.<server>`, served by the
    module `__getattr__`), and the servers come from `$AVA_HOME/mcp.json` and the plugins'
    bundled `.mcp.json`, so the list is live rather than a literal. Only this list decides what
    `help()` and the metering wrappers see; a new public name or submodule here widens nothing.
    """

    @property
    def __all_for_ava__(self) -> list[str]:
        """The configured server names and the two helpers, sorted."""
        return sorted([*servers(), "servers", "description"])


sys.modules[__name__].__class__ = _McpsModule
