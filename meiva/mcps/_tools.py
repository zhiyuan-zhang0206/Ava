"""The tool plumbing behind `meiva.mcps`. Private on purpose: `meiva.mcps.<name>` resolves to an MCP server
proxy, so a public submodule would shadow the server of the same name; only the `meiva/mcps` package
and its tests import it (the metering wrapper reaches it through `meiva.mcps.call_funnel`)."""

# `meiva/mcps/__init__.py` is the facade (the agent-visible helpers and the module `__getattr__`);
# it builds a `ToolSource` from the bound context's daemon client and hands it to everything
# here, so a test passes a source with a fake daemon client, a recording cache and a fixed
# clock.

from __future__ import annotations

import inspect
import json
import keyword
import time
import types
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from meiva.mcps.config import MCPCallError, MCPConnectError, ToolInfo
from meiva.security import scan_content

__all__ = [
    "DiskCache",
    "RemoteClient",
    "ServerProxy",
    "ToolCache",
    "ToolSource",
    "call_raw",
    "call_text",
    "list_tools",
    "make_tool_callable",
    "schema_to_signature",
]


class RemoteClient(Protocol):
    """The two daemon calls the tools make."""

    def list_tools(self, server: str, *, timeout_seconds: float) -> list[ToolInfo]: ...

    def call_tool(
        self, server: str, tool: str, args: dict[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]: ...


class ToolCache(Protocol):
    """Where a server's tool metadata is kept between calls."""

    def read(self, server: str) -> list[ToolInfo] | None: ...

    def write(self, server: str, tools: list[ToolInfo]) -> None: ...


@dataclass(frozen=True)
class ToolSource:
    """What a server proxy and its tools run against; dependencies only.

    `remote` and `timeout_seconds` are read at each call (the daemon client is built on first
    use and the timeout is a live setting), `cache` keeps tool metadata, and
    `validate_identity` is the external-identity check made before a tool call.
    """

    remote: Callable[[], RemoteClient]
    timeout_seconds: Callable[[], float]
    cache: ToolCache
    validate_identity: Callable[[], object]


@dataclass(frozen=True)
class DiskCache:
    """Tool metadata cache on disk: one JSON file per server under `directory()`."""

    directory: Callable[[], Path]
    now: Callable[[], float] = time.time
    ttl_s: float = 86400.0  # 24h

    def _path(self, server: str) -> Path:
        cache_dir = self.directory()
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{server}.json"

    def read(self, server: str) -> list[ToolInfo] | None:
        """Read disk cache for server tools. Returns None if expired or missing."""
        path = self._path(server)
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
            age = self.now() - cached_at
        except (TypeError, OverflowError):
            return None
        if age > self.ttl_s:
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

    def write(self, server: str, tools: list[ToolInfo]) -> None:
        """Write tool list to disk cache. Best-effort: errors are silently ignored."""
        path = self._path(server)
        data = {
            "tools": tools,
            "cached_at": self.now(),
        }
        with suppress(OSError):
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_tools(source: ToolSource, server: str) -> list[ToolInfo]:
    """Load daemon metadata, using the disk cache on connection failure."""
    try:
        tools = source.remote().list_tools(server, timeout_seconds=source.timeout_seconds())
    except (MCPConnectError, OSError):
        cached = source.cache.read(server)
        if cached is None:
            raise
        return cached
    source.cache.write(server, tools)
    return tools


def call_raw(source: ToolSource, server: str, tool: str, **args: Any) -> dict[str, Any]:
    """Call through the daemon once; an unknown result is never replayed."""
    source.validate_identity()
    return source.remote().call_tool(server, tool, args, timeout_seconds=source.timeout_seconds())


def call_text(source: ToolSource, server: str, tool: str, **args: Any) -> str:
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
    raw = call_raw(source, server, tool, **args)
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
# `meiva.mcps.<server>` goes through module-level `__getattr__`, returning
# `ServerProxy` (a `types.ModuleType` subclass so inspect.ismodule = True
# and meiva.help() walks it naturally).
#
# `meiva.mcps.<server>.<tool>` goes through ServerProxy.__getattr__,
# returning a callable (with `__name__` / `__doc__` set so
# inspect.isroutine = True and it's visible in help).


class ServerProxy(types.ModuleType):
    """Namespace for a single MCP server — `meiva.mcps.<server>`.

    Attribute access lazily returns a tool callable; `dir(proxy)` lists
    the server's tools. `__all_for_meiva__` reads disk cache first; only on cache
    miss does it connect — so `meiva.help()` doesn't trigger server startup.
    """

    def __init__(self, server: str, source: ToolSource) -> None:
        super().__init__(f"meiva.mcps.{server}")
        self._source = source
        self.__doc__ = f"Tools of MCP server {server!r}."
        self._server = server
        self._tools_cache: dict[str, ToolInfo] | None = None

    @property
    def __all_for_meiva__(self) -> list[str]:
        """`meiva.help()` checks `__all_for_meiva__` first — prefer disk cache, don't trigger connect."""
        return self._load_tool_names()

    def _load_tool_names(self) -> list[str]:
        """Get tool name list — prefer disk cache, fall back to _load_tools()."""
        if self._tools_cache is not None:
            return sorted(self._tools_cache.keys())
        cached = self._source.cache.read(self._server)
        if cached is not None:
            self._tools_cache = {t["name"]: t for t in cached}
            return sorted(self._tools_cache.keys())
        return sorted(self._load_tools().keys())

    def _load_tools(self) -> dict[str, ToolInfo]:
        if self._tools_cache is None:
            tools = list_tools(self._source, self._server)
            self._tools_cache = {t["name"]: t for t in tools}
        return self._tools_cache

    def __getattr__(self, name: str) -> Any:
        # ModuleType.__getattr__ is only called on ordinary lookup failure;
        # _server / _tools_cache are set in __init__ and go through instance
        # __dict__, not reaching here
        if name.startswith("_"):
            raise AttributeError(name)
        return make_tool_callable(self._source, self._server, name, self._load_tools().get(name))

    def __dir__(self) -> list[str]:
        return sorted([*self._load_tool_names(), "raw"])

    def raw(self, tool: str, **args: Any) -> dict[str, Any]:
        """Call a tool and return the full result for non-text outputs: a dict
        with `content` (list of blocks), `isError`, `structuredContent`."""
        return call_raw(self._source, self._server, tool, **args)


# JSON Schema primitive `type` -> Python type, for a display-only signature.
JSON_PRIMITIVE: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def make_tool_callable(source: ToolSource, server: str, tool: str, info: ToolInfo | None) -> Any:
    """Return a sync callable wrapping the tool; `__doc__` contains description + input_schema.

    When info=None (tool name not in cache), still returns a callable —
    the MCP server itself rejects bad names; the error info is more reliable.
    """

    def call(**kwargs: Any) -> str:
        return call_text(source, server, tool, **kwargs)

    call.__name__ = tool
    call.__qualname__ = f"meiva.mcps.{server}.{tool}"
    desc = (info or {}).get("description") or ""
    schema = (info or {}).get("input_schema") or {}
    sig = schema_to_signature(schema)
    if sig is not None:
        # Display only: `help()` reads this; the call still goes through **kwargs.
        call.__signature__ = sig  # pyright: ignore[reportFunctionMemberAccess]
    parts = [desc] if desc else []
    if schema:
        parts.append(f"\nInput schema (JSON):\n{json.dumps(schema, ensure_ascii=False, indent=2)}")
    parts.append(
        f"\nFor a non-text result (image / structured) use `meiva.mcps.{server}.raw({tool!r}, ...)`."
    )
    call.__doc__ = "\n".join(parts) if parts else f"Call {server}.{tool}"
    return call


def schema_to_signature(schema: dict[str, Any]) -> inspect.Signature | None:
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
        ann = JSON_PRIMITIVE.get(ptype, Any) if isinstance(ptype, str) else Any
        default = inspect.Parameter.empty if name in required else None
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default)
        )
    return inspect.Signature(params, return_annotation=str)
