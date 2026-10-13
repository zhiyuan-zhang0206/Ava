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

import inspect
import json
import keyword
import sys
import time
import types
from contextlib import suppress
from pathlib import Path
from typing import Any

from ava.mcps.config import (
    MCPCallError,
    MCPConnectError,
    MCPServerNotFound,
    ToolInfo,
    load_mcp_config,
)
from ava.mcps.config import (
    MCPError as MCPError,
)
from ava.mcps.config import (
    MCPToolNotFound as MCPToolNotFound,
)
from ava.sdk_surface import process_context
from ava.sdk_surface.validation import coerce_str
from ava.security import scan_content

from ._clients import McpClients
from ._remote import (
    _current_agent_id as _current_agent_id,
)
from ._remote import (
    _daemon_socket_path as _daemon_socket_path,
)

# The MCP daemon socket client lives in `._remote` (#1229); `_get_remote_client` (below) is what
# `_list_tools` / `_call_raw` consult. The names are re-exported so tests and help() keep reaching
# them here.
from ._remote import (
    _RemoteMCPClient as _RemoteMCPClient,
)
from ._remote import (
    _socket_path_for as _socket_path_for,
)

# Disk cache TTL
_CACHE_TTL_S = 86400.0  # 24h


def _cache_dir() -> Path:
    """Disk cache directory for tool metadata. Same `_config_path` route through ava_home()."""
    from base.paths import ava_home

    return ava_home() / "mcp_cache"


def _load_config() -> dict[str, dict[str, Any]]:
    """Merged MCP server map (machine `$AVA_HOME/mcp.json` + plugin-bundled `.mcp.json`).

    Returns an empty dict when nothing is configured.

    Raises:
        MCPError: a source file fails to parse / has a non-dict `mcpServers`.
    """
    return load_mcp_config()


def servers() -> list[str]:
    return sorted(_load_config().keys())


def description(server: str) -> str | None:
    """One-line summary of a configured server, or None when it declares
    none."""
    server = coerce_str(server, "server")
    config = _load_config()
    if server not in config:
        raise MCPServerNotFound(f"ava.mcps has no server {server!r} (have: {sorted(config)})")
    return config[server].get("description")


# ─── Disk cache for tool metadata ───────────────────────────────────────────


def _cache_path(server: str) -> Path:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{server}.json"


def _read_cache(server: str) -> list[ToolInfo] | None:
    """Read disk cache for server tools. Returns None if expired or missing."""
    path = _cache_path(server)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    cached_at = data.get("cached_at")
    if cached_at is None:
        return None
    try:
        age = time.time() - cached_at
    except (TypeError, OverflowError):
        return None
    if age > _CACHE_TTL_S:
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None
    result: list[ToolInfo] = []
    for t in tools:
        if not isinstance(t, dict):
            return None
        result.append(
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {}),
            }
        )
    return result


def _write_cache(server: str, tools: list[ToolInfo]) -> None:
    """Write tool list to disk cache. Best-effort: errors are silently ignored."""
    path = _cache_path(server)
    data = {
        "tools": tools,
        "cached_at": time.time(),
    }
    with suppress(OSError):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clients() -> McpClients:
    """The daemon client of the bound context, built on first use."""

    return process_context.current_context().clients.get(McpClients)


def _get_remote_client() -> _RemoteMCPClient:
    """Require this machine's MCP daemon; the SDK never starts servers."""
    return _clients().remote()


def _list_tools(server: str) -> list[ToolInfo]:
    """Load daemon metadata, using the disk cache on connection failure."""
    try:
        tools = _get_remote_client().list_tools(
            server, timeout_seconds=_clients().timeout_seconds()
        )
    except (MCPConnectError, OSError):
        cached = _read_cache(server)
        if cached is None:
            raise
        return cached
    _write_cache(server, tools)
    return tools


def _call_raw(server: str, tool: str, **args: Any) -> dict[str, Any]:
    """Call through the daemon once; an unknown result is never replayed."""
    from ava.sdk_surface import agent_identity

    agent_identity.validate_external_identity()
    return _get_remote_client().call_tool(
        server, tool, args, timeout_seconds=_clients().timeout_seconds()
    )


def _call_text(server: str, tool: str, **args: Any) -> str:
    """Call tool, join text content blocks into one str.

    isError=True → raise MCPCallError. An empty text join is returned as "" when
    the response is text-only (a list tool with nothing to list is a legitimate
    ""). When the join is empty AND the response also carries data a text join
    cannot represent — a non-text block (image/resource), or a structuredContent
    payload — it raises instead, pointing the caller at `.raw` so that data is
    not silently dropped. (A present structuredContent triggers this even
    alongside empty text blocks: the text is "" but the real payload is in
    structuredContent.)
    """
    raw = _call_raw(server, tool, **args)
    text_parts = [c["text"] for c in raw["content"] if c.get("type") == "text"]
    text = "\n".join(text_parts)
    if raw["isError"]:
        raise MCPCallError(f"{server}.{tool} errored: {text or '(no message)'}")
    if not text:
        # An empty join is only an error when something a text join cannot
        # represent is present. A non-text block (image/resource) means the
        # real payload is elsewhere; a structured-only response likewise. But a
        # response whose blocks are ALL text — even if each is the empty string
        # (e.g. chrome list_pages with no open pages) — is a genuine "".
        non_text = [c for c in raw["content"] if c.get("type") != "text"]
        if non_text:
            kinds = [c.get("type", "?") for c in raw["content"]]
            raise MCPCallError(
                f"{server}.{tool} returned non-text content (kinds={kinds}); use .raw() to get the full result"
            )
        if raw["structuredContent"] is not None:
            raise MCPCallError(
                f"{server}.{tool} returned only structuredContent; use .raw() to get the full result"
            )
    # A tool result (browser page text, fetched document, remote API payload) is
    # untrusted ingested content, so it is scanned before the agent reads it.
    return scan_content(text, source=f"mcps.{server}.{tool}")


# ─── Namespace proxy ──────────────────────────────────────────────────────
#
# `ava.mcps.<server>` goes through module-level `__getattr__`, returning
# `_ServerProxy` (a `types.ModuleType` subclass so inspect.ismodule = True
# and ava.help() walks it naturally).
#
# `ava.mcps.<server>.<tool>` goes through _ServerProxy.__getattr__,
# returning a callable (with `__name__` / `__doc__` set so
# inspect.isroutine = True and it's visible in help).


class _ServerProxy(types.ModuleType):
    """Namespace for a single MCP server — `ava.mcps.<server>`.

    Attribute access lazily returns a tool callable; `dir(proxy)` lists
    the server's tools. `__all_for_ava__` reads disk cache first; only on cache
    miss does it connect — so `ava.help()` doesn't trigger server startup.
    """

    def __init__(self, server: str) -> None:
        super().__init__(f"ava.mcps.{server}")
        self.__doc__ = f"Tools of MCP server {server!r}."
        self._server = server
        self._tools_cache: dict[str, ToolInfo] | None = None

    @property
    def __all_for_ava__(self) -> list[str]:
        """`ava.help()` checks `__all_for_ava__` first — prefer disk cache, don't trigger connect."""
        return self._load_tool_names()

    def _load_tool_names(self) -> list[str]:
        """Get tool name list — prefer disk cache, fall back to _load_tools()."""
        if self._tools_cache is not None:
            return sorted(self._tools_cache.keys())
        cached = _read_cache(self._server)
        if cached is not None:
            self._tools_cache = {t["name"]: t for t in cached}
            return sorted(self._tools_cache.keys())
        return sorted(self._load_tools().keys())

    def _load_tools(self) -> dict[str, ToolInfo]:
        if self._tools_cache is None:
            tools = _list_tools(self._server)
            self._tools_cache = {t["name"]: t for t in tools}
        return self._tools_cache

    def __getattr__(self, name: str) -> Any:
        # ModuleType.__getattr__ is only called on ordinary lookup failure;
        # _server / _tools_cache are set in __init__ and go through instance
        # __dict__, not reaching here
        if name.startswith("_"):
            raise AttributeError(name)
        return _make_tool_callable(self._server, name, self._load_tools().get(name))

    def __dir__(self) -> list[str]:
        return sorted([*self._load_tool_names(), "raw"])

    def raw(self, tool: str, **args: Any) -> dict[str, Any]:
        """Call a tool and return the full result for non-text outputs: a dict
        with `content` (list of blocks), `isError`, `structuredContent`."""
        return _call_raw(self._server, tool, **args)


# JSON Schema primitive `type` -> Python type, for a display-only signature.
_JSON_PRIMITIVE: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _schema_to_signature(schema: dict[str, Any]) -> inspect.Signature | None:
    """Build a display-only signature from a tool's JSON Schema, so the tool
    renders as `(*, url: str, ...)` instead of `(**kwargs)` when listed.

    Returns None (caller keeps the `(**kwargs)` signature) when the schema has no
    usable `properties`, or any property name is not a plain Python identifier.
    This does no validation and is never consulted on the call path — required vs
    optional is conveyed only by the presence or absence of a default. So a wrong
    type or a missing field still fails at the server, not here (fail-fast).
    """
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    # An MCP tool parameter name can be a non-identifier (hyphen, leading digit)
    # or a Python keyword (`from`, `class`); inspect.Parameter rejects or
    # mis-renders those. Fall back to (**kwargs) — the JSON schema in the
    # docstring still lists every parameter.
    if not all(isinstance(n, str) and n.isidentifier() and not keyword.iskeyword(n) for n in props):
        return None
    required = set(schema.get("required") or [])
    params: list[inspect.Parameter] = []
    for name, pschema in props.items():
        ptype = pschema.get("type") if isinstance(pschema, dict) else None
        ann = _JSON_PRIMITIVE.get(ptype, Any) if isinstance(ptype, str) else Any
        default = inspect.Parameter.empty if name in required else None
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default)
        )
    return inspect.Signature(params, return_annotation=str)


def _make_tool_callable(server: str, tool: str, info: ToolInfo | None) -> Any:
    """Return a sync callable wrapping the tool; `__doc__` contains description + input_schema.

    When info=None (tool name not in cache), still returns a callable —
    the MCP server itself rejects bad names; the error info is more reliable.
    """

    def call(**kwargs: Any) -> str:
        return _call_text(server, tool, **kwargs)

    call.__name__ = tool
    call.__qualname__ = f"ava.mcps.{server}.{tool}"
    desc = (info or {}).get("description") or ""
    schema = (info or {}).get("input_schema") or {}
    sig = _schema_to_signature(schema)
    if sig is not None:
        # Display only: `help()` reads this; the call still goes through **kwargs.
        call.__signature__ = sig  # pyright: ignore[reportFunctionMemberAccess]
    parts = [desc] if desc else []
    if schema:
        parts.append(f"\nInput schema (JSON):\n{json.dumps(schema, ensure_ascii=False, indent=2)}")
    parts.append(
        f"\nFor a non-text result (image / structured) use `ava.mcps.{server}.raw({tool!r}, ...)`."
    )
    call.__doc__ = "\n".join(parts) if parts else f"Call {server}.{tool}"
    return call


def __getattr__(name: str) -> Any:
    """`ava.mcps.<server>` lazily returns server proxy (module-level PEP 562)."""
    if name.startswith("_"):
        raise AttributeError(name)
    if name not in _load_config():
        raise AttributeError(
            f"ava.mcps has no server {name!r} (not configured in `$AVA_HOME/mcp.json`; have: {servers()})"
        )
    return _ServerProxy(name)


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
