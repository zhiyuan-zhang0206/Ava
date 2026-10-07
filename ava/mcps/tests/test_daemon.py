"""Unit tests for ava.mcps._daemon — JSON-line protocol / session cache / lifecycle.

Does not start real MCP servers (requires external npm/uvx packages) nor long-running daemon processes.
Three layers of mock:
- `ava_home()` → tmpdir, so `_load_config()` reads a fake `mcp.json`
- `_connect_server` → returns a MagicMock session, bypassing stdio_client / ClientSession
- `_handle_client` runs with in-memory `asyncio.StreamReader` + a custom fake writer

At least one real socket E2E (`asyncio.start_unix_server` ↔ `asyncio.open_unix_connection`)
is kept as a minimal smoke test, verifying lifecycle/clean-up; not repeated in every test case.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps._daemon as daemon_mod

# Most tests here are deterministic (mocked I/O / pure DB side-effects) and run in
# the parallel pool. Only the real AF_UNIX socket lifecycle smoke tests depend on
# real wall-clock timing (await-until-ready poll); they keep `@pytest.mark.flaky`
# to run serial. The retry tests below mock `asyncio.sleep`, so their backoff is
# instant and deterministic — the backoff *schedule* is verified separately by
# `test_handle_client_retry_sleeps_exponential_backoff`.

# ─── helpers ─────────────────────────────────────────────────────────────


class _FakeWriter:
    """Minimal asyncio.StreamWriter mock — accumulates written bytes, drain/close are noops.

    Uses list to accumulate chunks + helper to parse all response lines back into dicts,
    making assertions more convenient.
    `_handle_client` only uses write / drain / close / wait_closed four APIs,
    Pyright sees the full StreamWriter protocol and complains — pass cast when calling.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self.closed = False
        self.wait_closed_called = False

    def write(self, data: bytes) -> None:
        self._chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True

    def responses(self) -> list[dict[str, Any]]:
        raw = b"".join(self._chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line]


def _writer_arg(w: _FakeWriter) -> asyncio.StreamWriter:
    """Pyright sees `_FakeWriter` is not a `StreamWriter` subclass and complains;
    `_handle_client` actually only duck-types 4 methods, force cast here to silence the warning."""
    return w  # type: ignore[return-value]


def _make_reader(lines: list[bytes]) -> asyncio.StreamReader:
    """build StreamReader fed lines + EOF, so `await reader.readline()` returns them in order."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


_ORIG_REAP = daemon_mod._reap_stale_daemons


@pytest.fixture(autouse=True)
def _no_reap_stale_daemons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep run_daemon tests from scanning the real process table.

    `_reap_stale_daemons` walks psutil.process_iter on every daemon start; the
    reap behavior itself is unit-tested with a mocked process table (see
    `test_reap_stale_daemons_kills_only_this_unit`), which restores the real
    function first.
    """
    monkeypatch.setattr(daemon_mod, "_reap_stale_daemons", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def daemon_wide() -> daemon_mod._DaemonWide:
    """The daemon-wide state of one fresh daemon (what `run_daemon` builds once)."""
    return daemon_mod._DaemonWide()


@pytest.fixture
def scope(daemon_wide: daemon_mod._DaemonWide) -> daemon_mod._Scope:
    """One client connection's scope: its own empty buckets over the daemon-wide state."""
    return daemon_mod._Scope(
        local=daemon_mod._Buckets(), shared=daemon_wide.shared, oauth_locks=daemon_wide.oauth_locks
    )


@pytest.fixture
def fake_home(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `ava_home()` to tmpdir, so `_load_config()` reads a fake mcp.json.

    Patches builtin_mcp_paths to list so tests expecting empty config are
    not surprised by the repo's mcps/chrome/.mcp.json built-in.
    """
    import ava.mcp_config as _cfg

    monkeypatch.setattr(_cfg, "builtin_mcp_paths", list)
    return unit_home


def _write_config(home: Path, servers: dict[str, dict[str, Any]]) -> Path:
    cfg = home / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return cfg


def _make_session(
    *,
    tools: list[Any] | None = None,
    call_result: Any = None,
    call_error: Exception | None = None,
) -> MagicMock:
    """build a mock MCP ClientSession, with async list_tools / call_tool."""
    session = MagicMock(name="MCPSession")
    session.list_tools = AsyncMock(return_value=MagicMock(tools=tools or []))
    if call_error is not None:
        session.call_tool = AsyncMock(side_effect=call_error)
    else:
        session.call_tool = AsyncMock(return_value=call_result)
    return session


def _tool(name: str, description: str = "", schema: dict | None = None) -> MagicMock:
    t = MagicMock(spec=["name", "description", "input_schema"])
    t.name = name
    t.description = description
    t.input_schema = schema or {}
    return t


def _content(payload: dict[str, Any]) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value=payload)
    return c


def _call_result(
    content: list[Any], *, is_error: bool = False, structured: dict | None = None
) -> MagicMock:
    r = MagicMock()
    r.content = content
    r.is_error = is_error
    r.structured_content = structured
    return r


# ─── _load_config (delegates to the shared loader) ───────────────────────


def test_load_config_empty_when_no_file(fake_home: Path) -> None:
    assert daemon_mod._load_config() == {}


def test_load_config_empty_when_no_mcp_servers_key(fake_home: Path) -> None:
    """File exists but lacks mcpServers section → empty dict (tolerates empty settings file)."""
    (fake_home / "mcp.json").write_text(json.dumps({"other": {}}), encoding="utf-8")
    assert daemon_mod._load_config() == {}


def test_load_config_returns_parsed_servers(fake_home: Path) -> None:
    _write_config(fake_home, {"fs": {"command": "x"}, "chrome": {"command": "y"}})
    cfg = daemon_mod._load_config()
    assert set(cfg.keys()) == {"fs", "chrome"}
    assert cfg["fs"] == {"command": "x"}


# ─── _get_session ────────────────────────────────────────────────────────


async def test_get_session_lazy_inits_and_caches(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """First get calls _connect_server once, second directly returns cached (no further call)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    connect = AsyncMock(return_value=(session, stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    s1 = await daemon_mod._get_session("fs", scope)
    s2 = await daemon_mod._get_session("fs", scope)
    assert s1 is session and s2 is session
    connect.assert_awaited_once_with("fs", scope.oauth_locks)
    assert scope.local.sessions["fs"] is session
    assert scope.local.stacks["fs"] is stack


async def test_get_session_raises_on_unknown_server(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """server not configured in mcp.json → ValueError immediately blows up (fail-fast, no silent connect)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock())
    with pytest.raises(ValueError, match="nope"):
        await daemon_mod._get_session("nope", scope)


async def test_get_session_concurrent_calls_share_one_connect(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """Multiple concurrent _get_session(same server) trigger only one _connect_server (lock convergence).

    Simulate two clients simultaneously listing tools for the same server without duplicate subprocess launch.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    call_count = 0
    started = asyncio.Event()

    async def slow_connect(_server: str, _oauth_locks: dict[str, Any]) -> tuple[Any, Any]:
        nonlocal call_count
        call_count += 1
        started.set()
        # simulate slow connect, making the second coroutine enter lock wait
        await asyncio.sleep(0.05)
        return session, stack

    monkeypatch.setattr(daemon_mod, "_connect_server", slow_connect)

    t1 = asyncio.create_task(daemon_mod._get_session("fs", scope))
    await started.wait()  # ensure t1 has entered connect
    t2 = asyncio.create_task(daemon_mod._get_session("fs", scope))
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 is session and r2 is session
    assert call_count == 1


# ─── _handle_client: JSON-line protocol ──────────────────────────────────────


async def test_handle_client_lists_tools(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        tools=[_tool("read_file", "Read a file", {"type": "object"}), _tool("write_file")]
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()

    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["id"] == 1
    assert resp["ok"] is True
    assert resp["result"] == [
        {"name": "read_file", "description": "Read a file", "input_schema": {"type": "object"}},
        {"name": "write_file", "description": "", "input_schema": {}},
    ]
    assert writer.closed
    assert writer.wait_closed_called


async def test_handle_client_call_tool_returns_content(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result(
            [_content({"type": "text", "text": "hi"})],
            is_error=False,
            structured={"k": 1},
        ),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {"path": "/x"}},
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp == {
        "id": 7,
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": "hi"}],
            "isError": False,
            "structuredContent": {"k": 1},
        },
    }
    session.call_tool.assert_awaited_once_with("read", {"path": "/x"})


async def test_handle_client_call_tool_carries_is_error_true(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """tool returns isError=True → daemon still ok=True and returns the full structure,
    error judgment left to the client.

    Consistent with the ava.mcps client contract: daemon layer only transports,
    semantic layer (`is_error → raise`) is implemented on the client side."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result([_content({"type": "text", "text": "denied"})], is_error=True),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "rm"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    assert resp["result"]["isError"] is True


async def test_handle_client_call_tool_unknown_content_block_raises_type_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """ContentBlock is not a pydantic model (no model_dump) → outer try catch → ok=False.

    fail-fast prevents daemon from silently swallowing into {"type":"unknown"}; agent sees the error
    and can check if MCP SDK was upgraded."""
    _write_config(fake_home, {"fs": {"command": "x"}})

    class _BadBlock:
        """No model_dump deliberately triggers TypeError branch."""

    session = _make_session(call_result=_call_result([_BadBlock()]))
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 2, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["id"] == 2
    assert resp["ok"] is False
    assert "TypeError" in resp["error"]
    assert "MCP content block" in resp["error"]


async def test_handle_client_bad_json_returns_parse_error(
    fake_home: Path, scope: daemon_mod._Scope
) -> None:
    """JSON line parse failure → ok=False with 'JSON parse error', id is None."""
    reader = _make_reader([b"not json {{{\n"])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["id"] is None
    assert resp["ok"] is False
    assert "JSON parse error" in resp["error"]


async def test_handle_client_continues_after_bad_json(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """After bad JSON, the next good list_tools still responds normally — line-by-line resync, no broken connection."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(tools=[_tool("ok")])
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    reader = _make_reader(
        [
            b"garbage\n",
            (
                json.dumps({"id": 99, "method": "list_tools", "params": {"server": "fs"}}) + "\n"
            ).encode(),
        ]
    )
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    r1, r2 = writer.responses()
    assert r1["ok"] is False and "JSON parse error" in r1["error"]
    assert r2 == {
        "id": 99,
        "ok": True,
        "result": [{"name": "ok", "description": "", "input_schema": {}}],
    }


async def test_handle_client_unknown_method(fake_home: Path, scope: daemon_mod._Scope) -> None:
    req = {"id": 3, "method": "nuke_database", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp == {"id": 3, "ok": False, "error": "Unknown method: nuke_database"}


async def test_handle_client_eof_closes_writer(fake_home: Path, scope: daemon_mod._Scope) -> None:
    """Empty readline (client disconnects) → break out of loop, finally close writer."""
    reader = _make_reader([])  # immediate EOF
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    assert writer.responses() == []
    assert writer.closed
    assert writer.wait_closed_called


async def test_handle_client_connection_reset_is_suppressed(
    fake_home: Path, scope: daemon_mod._Scope
) -> None:
    """readline raises ConnectionResetError → not re-raised, finally still closes writer."""

    class _ResettingReader:
        async def readline(self) -> bytes:
            raise ConnectionResetError("peer reset")

    writer = _FakeWriter()
    await daemon_mod._handle_client(
        _ResettingReader(),  # type: ignore[arg-type]
        _writer_arg(writer),
        scope,
    )
    assert writer.closed


async def test_handle_client_call_tool_session_call_failure_propagates_as_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """upstream session.call_tool raise → outer try → ok=False (daemon does not die)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(call_error=RuntimeError("upstream boom"))
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 4, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["id"] == 4
    assert resp["ok"] is False
    assert "RuntimeError" in resp["error"]
    assert "upstream boom" in resp["error"]


async def test_handle_client_default_server_param_empty_string(
    fake_home: Path, scope: daemon_mod._Scope
) -> None:
    """Missing server param → goes to _get_session('') → ValueError → ok=False.

    Verify the default path does not silently swallow errors — empty server name treated as unconfigured.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})

    req = {"id": 5, "method": "list_tools", "params": {}}  # didn't pass server
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["id"] == 5
    assert resp["ok"] is False
    assert "not configured" in resp["error"]


async def test_handle_client_response_content_empty_when_no_blocks(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """tool returns empty content → ok=True with empty list (no raise)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(call_result=_call_result([], structured=None))
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 6, "method": "call_tool", "params": {"server": "fs", "tool": "noop"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert resp["result"]["content"] == []


# ─── _cleanup ────────────────────────────────────────────────────────────


async def test_cleanup_closes_all_stacks_and_clears_state() -> None:
    """Close each stack + clear both dicts; stack.aclose raises error also suppressed to continue clearing the next."""
    s1 = MagicMock()
    s1.aclose = AsyncMock()
    s2 = MagicMock()
    s2.aclose = AsyncMock(
        side_effect=RuntimeError("close fail")
    )  # still must continue to aclose s3
    s3 = MagicMock()
    s3.aclose = AsyncMock()

    buckets = daemon_mod._Buckets(
        sessions={"a": "fake_session_a", "b": "fake_session_b", "c": "fake_session_c"},
        stacks={"a": s1, "b": s2, "c": s3},
    )

    await buckets.close()
    s1.aclose.assert_awaited_once()
    s2.aclose.assert_awaited_once()
    s3.aclose.assert_awaited_once()
    assert buckets.sessions == {}
    assert buckets.stacks == {}


# ─── run_daemon: real Unix socket launch a list_tools, verify lifecycle ────────


@pytest.fixture
def short_socket_path() -> Iterator[str]:
    """macOS AF_UNIX path limit is 104 chars; pytest tmp_path too long to use.
    Use `/tmp/<pid>_<rand>.sock` self-managed within the test, finally unlink."""
    sock = str(Path(tempfile.gettempdir()) / f"avadaemon_{os.getpid()}_{os.urandom(3).hex()}.sock")
    try:
        yield sock
    finally:
        with contextlib.suppress(OSError):
            Path(sock).unlink()


async def _wait_for_daemon_socket(
    daemon_task: asyncio.Task, socket_path: str, *, timeout: float = 3.0
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wait for daemon to listen, return the connected (reader, writer).

    Each loop also checks daemon_task.done() — if startup fails, surface the exception immediately,
    otherwise the caller only sees a vague 'daemon never listened on socket'.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if daemon_task.done():
            exc = daemon_task.exception()
            raise RuntimeError(f"daemon exited during startup: {exc!r}")
        if Path(socket_path).exists() and Path(socket_path).is_socket():
            try:
                return await asyncio.open_unix_connection(socket_path)
            except (FileNotFoundError, ConnectionRefusedError):
                pass
        await asyncio.sleep(0.02)
    raise RuntimeError(f"daemon never listened on {socket_path}")


@pytest.mark.flaky  # real AF_UNIX socket lifecycle: await-until-ready poll
async def test_run_daemon_full_lifecycle_via_unix_socket(
    short_socket_path: str, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start daemon → connect socket → send list_tools → verify response → cancel → clean.

    A minimal real socket smoke test verifies:
    - `asyncio.start_unix_server` starts + accepts connection
    - JSON-line protocol runs through (`_handle_client` already unit-tested with fake reader/writer,
      here real socket integration)
    - stale file cleaned before binding again
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(tools=[_tool("ping")])
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    # pre-write a stale file to verify run_daemon startup cleans it
    Path(short_socket_path).write_text("stale")

    daemon_task = asyncio.create_task(daemon_mod.run_daemon(short_socket_path))
    reader, writer = await _wait_for_daemon_socket(daemon_task, short_socket_path, timeout=1.5)

    try:
        req = json.dumps({"id": 42, "method": "list_tools", "params": {"server": "fs"}})
        writer.write((req + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        resp = json.loads(line.decode())
        assert resp["id"] == 42
        assert resp["ok"] is True
        assert resp["result"][0]["name"] == "ping"
    finally:
        writer.close()
        await asyncio.sleep(0)  # let close() complete transport flush
        daemon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon_task


@pytest.mark.flaky  # real AF_UNIX socket lifecycle: await-until-ready poll
async def test_run_daemon_graceful_shutdown_closes_server_and_cleans_socket(
    short_socket_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop_event.set() → server.close() + cleanup + unlink socket → exits clean.

    Real SIGTERM/SIGINT signals would pollute the pytest runner; instead monkeypatch
    `asyncio.Event` to a spy instance capturing, explicit set in test triggers graceful path.
    """
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock())
    captured: list[asyncio.Event] = []

    class _SpyEvent(asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            captured.append(self)

    monkeypatch.setattr(daemon_mod.asyncio, "Event", _SpyEvent)

    daemon_task = asyncio.create_task(daemon_mod.run_daemon(short_socket_path))

    # wait for daemon to listen + get the stop_event instance
    for _ in range(75):
        if captured and Path(short_socket_path).exists() and Path(short_socket_path).is_socket():
            break
        await asyncio.sleep(0.02)
    assert captured, "daemon never built stop_event"

    # explicitly trigger shutdown
    captured[0].set()
    await asyncio.wait_for(daemon_task, timeout=2.0)

    # graceful path requirement: socket file cleaned up
    assert not Path(short_socket_path).exists()


# ─── main entry ────────────────────────────────────────────────────────────


# ─── socket ownership guards (Task #1142) ──────────────────────────────────


class _FakeProc:
    """A psutil.Process stand-in for the reaper: argv, cwd, env, and a kill flag."""

    def __init__(self, pid: int, cmdline: list[str], cwd: str, env: dict[str, str]) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._cwd = cwd
        self._env = env
        self.killed = False

    def cmdline(self) -> list[str]:
        return self._cmdline

    def cwd(self) -> str:
        return self._cwd

    def environ(self) -> dict[str, str]:
        return self._env

    def kill(self) -> None:
        self.killed = True


def _is_daemon_cmdline_cases() -> list[tuple[list[str], bool]]:
    """(cmdline, expected) pairs for `_is_daemon_cmdline`."""
    return [
        # The daemon's own launch shapes.
        ([".venv/bin/python", "-m", "ava.mcps._daemon"], True),
        ([".venv/bin/python", "-m", "ava.mcps._daemon", "/tmp/x.sock"], True),  # noqa: S108
        (["bash", "-lc", "cd /root && .venv/bin/python -m ava.mcps._daemon"], False),
        # A `bash -lc` wrapper: the whole launch command is ONE argv element
        # that contains the module name — no element equals it, never matched.
        (
            [
                "bash",
                "-lc",
                "cd /root && export VIRTUAL_ENV=/root/.venv && "
                'export PATH=/root/.venv/bin:"$PATH" && '
                ".venv/bin/python -m ava.mcps._daemon",
            ],
            False,
        ),
        # Same wrapper through `sh -c` (posixproc launches `sh -c "bash -lc ..."`).
        (["/bin/sh", "-c", "bash -lc 'cd /root && .venv/bin/python -m ava.mcps._daemon'"], False),
        # The module name as a plain argument (not after -m) is not a daemon.
        (["python", "-c", "import ava.mcps._daemon"], False),
        (["python", "ava.mcps._daemon"], False),
        # Unrelated process.
        (["python", "-m", "something.else"], False),
    ]


# ─── assert_requirements in connect path ─────────────────────────────────


# ─── _is_transport_error ─────────────────────────────────────────────────


# ─── _invalidate_session ─────────────────────────────────────────────────


# no exception raised


# ─── _handle_client retry ─────────────────────────────────────────────────


# ─── shared daemon: per-connection session isolation ─────────────────────


# ─── server subprocess sharing (`shared` spec) ────────────────────────────


# ─── _connect_server / _connect_http: remote (url) servers ───────────────


__all__ = ["_FakeProc"]
