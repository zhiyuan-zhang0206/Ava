"""Tools exposed by external MCP servers. Call `ava.mcps.<server>.<tool>(...)`
with the tool's named arguments."""

from __future__ import annotations

import asyncio
import inspect
import json
import keyword
import subprocess
import threading
import time
import types
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from ava._sdk_validation import coerce_str
from ava.security import scan_content
from shared.config import settings

from ._mcp_config import (
    MCPCallError,
    MCPConnectError,
    MCPError,
    MCPServerNotFound,
    MCPToolNotFound,
    ToolInfo,
    assert_requirements,
    is_transport_error,
    load_mcp_config,
    resolve_command,
    server_cwd,
    server_url,
)
from ._mcp_oauth import _OAUTH_FLOW_TIMEOUT_S
from ._mcp_remote import (
    _current_agent_id as _current_agent_id,
)
from ._mcp_remote import (
    _daemon_socket_path as _daemon_socket_path,
)
from ._mcp_remote import (
    _get_remote_client as _get_remote_client,
)

# MCP daemon socket client, moved to ._mcp_remote (2026-08-13 #1229);
# re-exported here as its historical home. `_get_remote_client` is what
# `_list_tools` / `_call_raw` consult; the rest keeps tests and help() stable.
from ._mcp_remote import (
    _RemoteMCPClient as _RemoteMCPClient,
)
from ._mcp_remote import (
    _socket_path_for as _socket_path_for,
)

# Disk cache TTL
_CACHE_TTL_S = 86400.0  # 24h


def _cache_dir() -> Path:
    """Disk cache directory for tool metadata. Same `_config_path` route through ava_home()."""
    from shared.paths import ava_home

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


# ─── sync↔async bridge + per-server session cache ───────────────────────
#
# MCP ClientSession / stdio_client are async-only. The Ava SDK is sync;
# cross over via a module-level daemon thread running an independent
# asyncio loop:
#   sync caller → asyncio.run_coroutine_threadsafe(coro, _loop).result()
#                                                       ↓
#                                  background thread runs _loop
#                                  ClientSessions live inside _loop
#
# Lifecycle: each session's stdio_client + ClientSession context enters
# AsyncExitStack; when subprocess exits, daemon thread dies, OS closes
# stdin pipe → compliant servers see EOF and graceful-exit. No explicit
# atexit close — closing stdio subprocesses in parent-process-dying
# scenarios is prone to deadlock.
#
# Concurrency: per-server asyncio.Lock prevents two callers from
# concurrently connecting to the same server and spawning two
# subprocesses. Failure cleanup: _connect uses a local AsyncExitStack
# for isolation; on failure aclose semi-initialized resources.

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_sessions: dict[str, Any] = {}  # server_name -> ClientSession (async-only)
_session_locks: dict[str, asyncio.Lock] = {}
# Per-server AsyncExitStack holding each stdio session's transport context, so a
# dead session can be closed individually for rebuild (the global _exit_stack
# below holds only stateless HTTP connections, which are never cached).
_session_stacks: dict[str, AsyncExitStack] = {}
_exit_stack: AsyncExitStack | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Lazy-start background thread + asyncio loop. Thread-safe double-check."""
    global _loop, _exit_stack  # noqa: PLW0603 — module-level lazy singleton
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()
        _exit_stack = AsyncExitStack()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        t = threading.Thread(target=_run, name="ava-mcps-loop", daemon=True)
        t.start()
        ready.wait()
        _loop = loop
        return loop


async def _connect(server: str, *, errlog: Any = None) -> Any:
    """Connect to the server in the background loop, return ClientSession (cache reused).

    errlog: stderr target passed to stdio_client.
      Default None → use mcp SDK default (sys.stderr).
      `subprocess.DEVNULL` → discard subprocess stderr (discovery scenario).
    """
    if server in _sessions:
        return _sessions[server]

    lock = _session_locks.get(server)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[server] = lock

    async with lock:
        if server in _sessions:
            return _sessions[server]

        cfg = _load_config()
        if server not in cfg:
            raise MCPServerNotFound(f"no server {server!r} in `$AVA_HOME/mcp.json`")
        spec = cfg[server]
        assert_requirements(spec)
        url = server_url(spec)
        if url is not None:
            return await _connect_http(url, spec.get("headers"), server=server)
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd:
            raise MCPError(f"server {server!r} 'command' field missing or not str")
        cmd = resolve_command(cmd)
        args_list = spec.get("args") or []
        env_dict = spec.get("env") or {}

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # Relative interpreter paths resolve against the child's cwd: installed
        # packages from their own dir, built-ins from the repo root.
        cwd = server_cwd(server)
        params = StdioServerParameters(command=cmd, args=args_list, env=env_dict or None, cwd=cwd)

        # errlog controls subprocess stderr destination:
        #   - Not passed → mcp SDK default sys.stderr (may pollute agent output)
        #   - DEVNULL → discard (discovery / _list_tools scenarios)
        stdio_kwargs: dict[str, Any] = {}
        if errlog is not None:
            stdio_kwargs["errlog"] = errlog

        # Failure path isolates cleanup via local stack; transfer only after full success
        local_stack = AsyncExitStack()
        try:
            read, write = await asyncio.wait_for(
                local_stack.enter_async_context(stdio_client(params, **stdio_kwargs)),
                timeout=settings.sandbox.mcp_connect_timeout_seconds,
            )
            session: Any = await local_stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    # Bound every request (call_tool / list_tools): the SDK
                    # default is no timeout, and a hung server would block the
                    # calling agent forever. Same knob the connect phase uses
                    # and the daemon request deadline applies.
                    read_timeout_seconds=settings.sandbox.mcp_connect_timeout_seconds,
                )
            )
            await asyncio.wait_for(
                session.initialize(), timeout=settings.sandbox.mcp_connect_timeout_seconds
            )
        except TimeoutError as e:
            await local_stack.aclose()
            raise MCPConnectError(
                f"connecting to server {server!r} timed out (>{settings.sandbox.mcp_connect_timeout_seconds}s)"
            ) from e
        except Exception as e:
            await local_stack.aclose()
            raise MCPConnectError(
                f"connecting to server {server!r} failed: {type(e).__name__}: {e}"
            ) from e

        # Keep the per-server stack so a dead session can be closed individually
        # and rebuilt (the old global stack made selective teardown impossible).
        _session_stacks[server] = local_stack
        _sessions[server] = session
        return session


async def _connect_http(
    url: str, headers: dict[str, str] | None, *, oauth: bool = False, server: str = ""
) -> Any:
    """Connect to a remote Streamable HTTP server in the background loop.

    Local-fallback counterpart of the daemon's `_connect_http`: dials the
    endpoint directly (no child process), same stateless semantics; `oauth=True` builds the browser authorization flow instead of static headers. Returns a
    ClientSession; the caller caches it in `_sessions`.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client,  # pyright: ignore[reportPrivateImportUsage] — re-exported from mcp.shared._httpx_utils
        streamable_http_client,
    )

    assert _exit_stack is not None  # noqa: S101 — _get_loop already initialized
    local_stack = AsyncExitStack()
    timeout = _OAUTH_FLOW_TIMEOUT_S if oauth else settings.sandbox.mcp_connect_timeout_seconds
    try:
        if oauth:
            from ._mcp_oauth import oauth_http_client

            http_client = await oauth_http_client(url, server)
        else:
            http_client = create_mcp_http_client(headers=headers) if headers else None
        read, write = await asyncio.wait_for(
            local_stack.enter_async_context(streamable_http_client(url, http_client=http_client)),
            timeout=timeout,
        )
        session: Any = await local_stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=settings.sandbox.mcp_connect_timeout_seconds,
            )
        )
        await asyncio.wait_for(session.initialize(), timeout=timeout)
    except TimeoutError as e:
        await local_stack.aclose()
        raise MCPConnectError(f"connecting to server {url!r} timed out (>{timeout}s)") from e
    except Exception as e:
        await local_stack.aclose()
        raise MCPConnectError(
            f"connecting to server {url!r} failed: {type(e).__name__}: {e}"
        ) from e
    await _exit_stack.enter_async_context(local_stack.pop_all())
    return session


def _run_async(coro: Any) -> Any:
    """Sync entry point — toss coro to the background loop and wait for result."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


async def _invalidate_session(server: str) -> None:
    """Close and drop `server`'s cached session — the stdio child died (server
    process restarted, pipe broke) and the cached ClientSession can never
    recover. Runs on the background loop; safe when uncached.

    Closes the per-server transport stack, which terminates the child process
    and frees its pipes, so the next `_connect` spawns a fresh one.
    """
    lock = _session_locks.get(server)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[server] = lock
    async with lock:
        _sessions.pop(server, None)
        stack = _session_stacks.pop(server, None)
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()


async def _call_with_reconnect(server: str, call: Any, *, errlog: Any = None) -> Any:
    """Run `call(session)` against the cached session; when the call fails with
    a transport error (dead stdio child / broken pipe), close the dead session,
    reconnect, and retry once.

    Tool-level errors (unknown tool, bad args, server-returned error) propagate
    untouched — retrying those would double-run side-effectful tools. At most
    one rebuild per call, so a server that cannot start still fails fast.
    """
    session = await _connect(server, errlog=errlog)
    try:
        return await call(session)
    except Exception as e:
        if not is_transport_error(e):
            raise
        await _invalidate_session(server)
        session = await _connect(server, errlog=errlog)
        return await call(session)


def _list_tools(server: str) -> list[ToolInfo]:
    """List the server's tools.

    Prefer the remote daemon path (when this agent's daemon is running); on
    failure fallback to cache/local. Next, disk cache (24h TTL). Finally,
    local lazy connect (writes cache).
    """
    remote = _get_remote_client()
    if remote is not None:
        # Only a transport failure (daemon dead / unreachable) falls back to
        # cache/local — a tool/server-level error (MCPCallError / MCPToolNotFound)
        # must propagate, not be silently retried on a freshly-spawned local
        # session, which would mask the error and double-run side effects.
        with suppress(MCPConnectError, OSError):
            return remote.list_tools(server)

    cached = _read_cache(server)
    if cached is not None:
        return cached

    async def _do() -> list[ToolInfo]:
        result = await _call_with_reconnect(
            server, lambda s: s.list_tools(), errlog=subprocess.DEVNULL
        )
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema or {},
            }
            for t in result.tools
        ]

    tools = _run_async(_do())
    _write_cache(server, tools)
    return tools


def _call_raw(server: str, tool: str, **args: Any) -> dict[str, Any]:
    """Call tool, return the full result dict ({content, isError, structuredContent})."""
    remote = _get_remote_client()
    if remote is not None:
        # Only a transport failure falls back to local; a tool/server-level error
        # (MCPCallError / MCPToolNotFound) propagates rather than being silently
        # retried locally (which would double-run a side-effectful tool).
        with suppress(MCPConnectError, OSError):
            return remote.call_tool(server, tool, args)

    async def _do() -> dict[str, Any]:
        try:
            result = await _call_with_reconnect(server, lambda s: s.call_tool(tool, args or None))
        except Exception as e:
            msg = str(e)
            if "tool" in msg.lower() and ("not found" in msg.lower() or "unknown" in msg.lower()):
                raise MCPToolNotFound(f"server {server!r} has no tool {tool!r}: {msg}") from e
            raise MCPCallError(f"calling {server}.{tool} failed: {type(e).__name__}: {e}") from e
        return {
            "content": [_dump_content(c) for c in (result.content or [])],
            "isError": bool(result.is_error),
            "structuredContent": result.structured_content,
        }

    return _run_async(_do())


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


def _dump_content(c: Any) -> dict[str, Any]:
    """Serialize an mcp.types ContentBlock into a plain dict.

    All compliant MCP content blocks are pydantic models with model_dump.
    Absence means SDK type change / we missed a new type — raise
    immediately; do not silently swallow as 'unknown' (fail-fast).
    """
    if not hasattr(c, "model_dump"):
        raise MCPError(f"Unrecognized MCP content block type: {type(c).__name__} ({c!r})")
    return c.model_dump(mode="python", exclude_none=True, by_alias=True)


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
    return [*servers(), "servers", "description", "help"]


def help() -> None:
    """Print a one-line summary of every configured MCP server."""
    names = servers()
    if not names:
        print("(no MCP servers configured)")
        return
    for name in names:
        cfg = _load_config()[name]
        cmd = cfg.get("command", "?")
        args_str = " ".join(cfg.get("args", []))
        print(f"  {name}/  # {cmd} {args_str}".rstrip())
