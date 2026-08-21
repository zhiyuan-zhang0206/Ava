"""Unit tests for ava._mcps_daemon — JSON-line protocol / session cache / lifecycle.

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
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ava._mcps_daemon as daemon_mod
from shared.config import settings

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


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Before/after each test, clear module-level sessions / stacks / session_locks.

    The daemon module uses module-level dicts for session caching (production: one daemon
    process holds one copy for its lifetime), tests must explicitly clear to avoid leaking
    mock state to the next test.
    """
    daemon_mod.sessions.clear()
    daemon_mod.stacks.clear()
    daemon_mod.session_locks.clear()
    daemon_mod.shared_sessions.clear()
    daemon_mod.shared_stacks.clear()
    daemon_mod.shared_locks.clear()
    yield
    daemon_mod.sessions.clear()
    daemon_mod.stacks.clear()
    daemon_mod.session_locks.clear()
    daemon_mod.shared_sessions.clear()
    daemon_mod.shared_stacks.clear()
    daemon_mod.shared_locks.clear()


@pytest.fixture
def fake_home(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `ava_home()` to tmpdir, so `_load_config()` reads a fake mcp.json.

    Patches _builtin_mcp_paths to list so tests expecting empty config are
    not surprised by the repo's mcps/chrome/.mcp.json built-in.
    """
    import ava._mcp_config as _cfg

    monkeypatch.setattr(_cfg, "_builtin_mcp_paths", list)
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
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First get calls _connect_server once, second directly returns cached (no further call)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    connect = AsyncMock(return_value=(session, stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    s1 = await daemon_mod._get_session(
        "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    s2 = await daemon_mod._get_session(
        "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert s1 is session and s2 is session
    connect.assert_awaited_once_with("fs")
    assert daemon_mod.sessions["fs"] is session
    assert daemon_mod.stacks["fs"] is stack


async def test_get_session_raises_on_unknown_server(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server not configured in mcp.json → ValueError immediately blows up (fail-fast, no silent connect)."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock())
    with pytest.raises(ValueError, match="nope"):
        await daemon_mod._get_session(
            "nope", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
        )


async def test_get_session_concurrent_calls_share_one_connect(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple concurrent _get_session(same server) trigger only one _connect_server (lock convergence).

    Simulate two clients simultaneously listing tools for the same server without duplicate subprocess launch.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    call_count = 0
    started = asyncio.Event()

    async def slow_connect(_server: str) -> tuple[Any, Any]:
        nonlocal call_count
        call_count += 1
        started.set()
        # simulate slow connect, making the second coroutine enter lock wait
        await asyncio.sleep(0.05)
        return session, stack

    monkeypatch.setattr(daemon_mod, "_connect_server", slow_connect)

    t1 = asyncio.create_task(
        daemon_mod._get_session(
            "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
        )
    )
    await started.wait()  # ensure t1 has entered connect
    t2 = asyncio.create_task(
        daemon_mod._get_session(
            "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
        )
    )
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 is session and r2 is session
    assert call_count == 1


# ─── _handle_client: JSON-line protocol ──────────────────────────────────────


async def test_handle_client_lists_tools(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
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
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
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
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    assert resp["result"]["isError"] is True


async def test_handle_client_call_tool_unknown_content_block_raises_type_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )

    [resp] = writer.responses()
    assert resp["id"] == 2
    assert resp["ok"] is False
    assert "TypeError" in resp["error"]
    assert "MCP content block" in resp["error"]


async def test_handle_client_bad_json_returns_parse_error(fake_home: Path) -> None:
    """JSON line parse failure → ok=False with 'JSON parse error', id is None."""
    reader = _make_reader([b"not json {{{\n"])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["id"] is None
    assert resp["ok"] is False
    assert "JSON parse error" in resp["error"]


async def test_handle_client_continues_after_bad_json(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )

    r1, r2 = writer.responses()
    assert r1["ok"] is False and "JSON parse error" in r1["error"]
    assert r2 == {
        "id": 99,
        "ok": True,
        "result": [{"name": "ok", "description": "", "input_schema": {}}],
    }


async def test_handle_client_unknown_method(fake_home: Path) -> None:
    req = {"id": 3, "method": "nuke_database", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp == {"id": 3, "ok": False, "error": "Unknown method: nuke_database"}


async def test_handle_client_eof_closes_writer(fake_home: Path) -> None:
    """Empty readline (client disconnects) → break out of loop, finally close writer."""
    reader = _make_reader([])  # immediate EOF
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    assert writer.responses() == []
    assert writer.closed
    assert writer.wait_closed_called


async def test_handle_client_connection_reset_is_suppressed(fake_home: Path) -> None:
    """readline raises ConnectionResetError → not re-raised, finally still closes writer."""

    class _ResettingReader:
        async def readline(self) -> bytes:
            raise ConnectionResetError("peer reset")

    writer = _FakeWriter()
    await daemon_mod._handle_client(
        _ResettingReader(),  # type: ignore[arg-type]
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    assert writer.closed


async def test_handle_client_call_tool_session_call_failure_propagates_as_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["id"] == 4
    assert resp["ok"] is False
    assert "RuntimeError" in resp["error"]
    assert "upstream boom" in resp["error"]


async def test_handle_client_default_server_param_empty_string(fake_home: Path) -> None:
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["id"] == 5
    assert resp["ok"] is False
    assert "not configured" in resp["error"]


async def test_handle_client_response_content_empty_when_no_blocks(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
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
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
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

    sessions = {"a": "fake_session_a", "b": "fake_session_b", "c": "fake_session_c"}
    stacks = {"a": s1, "b": s2, "c": s3}

    await daemon_mod._cleanup(sessions, stacks)  # pyright: ignore[reportUnknownMemberType]
    s1.aclose.assert_awaited_once()
    s2.aclose.assert_awaited_once()
    s3.aclose.assert_awaited_once()
    assert sessions == {}
    assert stacks == {}


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


@pytest.mark.flaky  # real AF_UNIX socket lifecycle: await-until-ready poll
async def test_run_daemon_clears_stale_socket_on_startup(
    short_socket_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing socket path (leftover from last crash) → run_daemon unlinks on startup then binds."""
    Path(short_socket_path).write_text("leftover")

    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock())
    daemon_task = asyncio.create_task(daemon_mod.run_daemon(short_socket_path))

    # being able to connect means bind succeeded = stale was cleaned
    _, writer = await _wait_for_daemon_socket(daemon_task, short_socket_path, timeout=1.5)
    writer.close()

    daemon_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await daemon_task


# ─── main entry ────────────────────────────────────────────────────────────


def test_main_requires_socket_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than one arg → exit 1, does not enter asyncio.run.

    Zero args is now the NORMAL shared-daemon mode (binds the per-machine
    socket); only an impossible argv (2+ positional) fails fast.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["_mcps_daemon", "/tmp/a.sock", "/tmp/b.sock"],  # noqa: S108
    )
    with patch.object(daemon_mod.asyncio, "run") as run_spy, pytest.raises(SystemExit) as exc:
        daemon_mod.main()
    assert exc.value.code == 1
    run_spy.assert_not_called()


def test_main_no_args_binds_shared_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """argv = [prog] → run_daemon(mcp_daemon_shared_socket()).

    The per-machine shared socket replaces the old one-daemon-per-agent argv
    contract: no argument means the ops-managed shared daemon.
    """
    monkeypatch.setattr(sys, "argv", ["_mcps_daemon"])
    # The shared-socket path may resolve onto a LIVE socket on a dev machine;
    # the live guard is unit-tested separately, so pin it off here.
    monkeypatch.setattr(daemon_mod, "_socket_is_live", lambda *_a: False)  # pyright: ignore[reportUnknownArgumentType]
    with patch.object(daemon_mod.asyncio, "run") as run_spy:
        run_spy.return_value = None
        daemon_mod.main()
    run_spy.assert_called_once()
    coro = run_spy.call_args.args[0]
    assert coro.__name__ == "run_daemon"
    coro.close()


def test_main_runs_daemon_with_socket_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """argv = [prog, sock_path] → calls asyncio.run(run_daemon(sock_path))."""
    monkeypatch.setattr(sys, "argv", ["_mcps_daemon", "/tmp/test.sock"])  # noqa: S108 — fake argv, not actually opened
    with patch.object(daemon_mod.asyncio, "run") as run_spy:
        # asyncio.run accepts coroutine — after patching it doesn't actually run (saves daemon startup overhead)
        run_spy.return_value = None
        daemon_mod.main()
    run_spy.assert_called_once()
    # verify the passed coroutine is run_daemon — assert by name, avoid depending on coroutine identity
    coro = run_spy.call_args.args[0]
    assert coro.__name__ == "run_daemon"
    coro.close()  # close the coroutine to avoid "never awaited" warning


# ─── socket ownership guards (Task #1142) ──────────────────────────────────


async def test_socket_is_live_true_when_ping_answered(short_socket_path: str) -> None:
    """A daemon answering the ping protocol = live; a second daemon must not start."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(b'{"ok": true}\n')
            await writer.drain()
            await asyncio.sleep(30)
        finally:
            writer.close()

    sock = short_socket_path
    server = await asyncio.start_unix_server(handler, path=sock)
    try:
        # `_socket_is_live` is a blocking sync socket call — run it off the
        # event loop so the server's handler can be scheduled (the production
        # call site, `main()`, is sync and has no such conflict).
        assert await asyncio.to_thread(daemon_mod._socket_is_live, sock) is True
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            Path(sock).unlink()


async def test_socket_is_live_false_when_connect_but_no_reply(short_socket_path: str) -> None:
    """A socket that accepts but never answers is NOT live — a half-dead occupant
    must be replaceable, not shielded forever by a successful connect."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(30)
        finally:
            writer.close()

    sock = short_socket_path
    server = await asyncio.start_unix_server(handler, path=sock)
    try:
        assert await asyncio.to_thread(daemon_mod._socket_is_live, sock) is False
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            Path(sock).unlink()


def test_socket_is_live_false_when_no_listener(short_socket_path: str) -> None:
    assert daemon_mod._socket_is_live(short_socket_path) is False


def test_unlink_own_socket_only_own_inode(tmp_path: Path) -> None:
    """The file is unlinked only while it is still the inode this daemon bound."""
    own = tmp_path / "own.sock"
    own.write_text("x")
    ino = own.stat().st_ino
    daemon_mod._unlink_own_socket(str(own), ino)
    assert not own.exists()

    # A later occupant replaced the file (different inode): leave it alone.
    replaced = tmp_path / "replaced.sock"
    replaced.write_text("x")
    stranger_ino = replaced.stat().st_ino
    daemon_mod._unlink_own_socket(str(replaced), stranger_ino + 1)
    assert replaced.exists()


def test_unlink_own_socket_missing_file_noop(tmp_path: Path) -> None:
    daemon_mod._unlink_own_socket(str(tmp_path / "nope.sock"), 123)
    assert not (tmp_path / "nope.sock").exists()


def test_reap_stale_daemons_kills_only_this_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only same-unit ghost daemons are reaped; self, other units, and
    non-daemon processes are never touched."""
    monkeypatch.setattr(daemon_mod, "_reap_stale_daemons", _ORIG_REAP)

    home = str(settings.general.ava_home)
    root = str(Path(daemon_mod.__file__).resolve().parent.parent)

    class FakeProc:
        def __init__(self, pid: int, cmdline: list[str], cwd: str, env: dict[str, str]) -> None:
            self.pid = pid
            self.info = {"cmdline": cmdline}
            self._cwd = cwd
            self._env = env
            self.killed = False

        def cwd(self) -> str:
            return self._cwd

        def environ(self) -> dict[str, str]:
            return self._env

        def kill(self) -> None:
            self.killed = True

    procs = [
        FakeProc(
            os.getpid(), [".venv/bin/python", "-m", "ava._mcps_daemon"], root, {"AVA_HOME": home}
        ),  # self
        FakeProc(
            1001, [".venv/bin/python", "-m", "ava._mcps_daemon"], root, {}
        ),  # same unit via cwd
        FakeProc(
            1002, [".venv/bin/python", "-m", "ava._mcps_daemon"], "/elsewhere", {"AVA_HOME": home}
        ),  # same via env
        FakeProc(
            1003,
            [".venv/bin/python", "-m", "ava._mcps_daemon"],
            "/other/root",
            {"AVA_HOME": "/other/home"},
        ),  # other unit
        FakeProc(
            1004, ["python", "-m", "something.else"], root, {"AVA_HOME": home}
        ),  # not a daemon
    ]
    with patch("psutil.process_iter", return_value=procs):
        daemon_mod._reap_stale_daemons(Path(root))

    assert procs[1].killed and procs[2].killed
    assert not procs[0].killed and not procs[3].killed and not procs[4].killed


def _is_daemon_cmdline_cases() -> list[tuple[list[str], bool]]:
    """(cmdline, expected) pairs for `_is_daemon_cmdline`."""
    return [
        # The daemon's own launch shapes.
        ([".venv/bin/python", "-m", "ava._mcps_daemon"], True),
        ([".venv/bin/python", "-m", "ava._mcps_daemon", "/tmp/x.sock"], True),  # noqa: S108
        # A `bash -lc` wrapper: the whole launch command is ONE argv element
        # that contains the module name — no element equals it, never matched.
        (
            [
                "bash",
                "-lc",
                "cd /root && export VIRTUAL_ENV=/root/.venv && "
                'export PATH=/root/.venv/bin:"$PATH" && '
                ".venv/bin/python -m ava._mcps_daemon",
            ],
            False,
        ),
        # Same wrapper through `sh -c` (posixproc launches `sh -c "bash -lc ..."`).
        (["/bin/sh", "-c", "bash -lc 'cd /root && .venv/bin/python -m ava._mcps_daemon'"], False),
        # The module name as a plain argument (not after -m) is not a daemon.
        (["python", "-c", "import ava._mcps_daemon"], False),
        (["python", "ava._mcps_daemon"], False),
        # Unrelated process.
        (["python", "-m", "something.else"], False),
    ]


def test_is_daemon_cmdline_discriminates_wrappers() -> None:
    """Only argv shaped `python -m ava._mcps_daemon` is a daemon launch."""
    for cmdline, expected in _is_daemon_cmdline_cases():
        assert daemon_mod._is_daemon_cmdline(cmdline) is expected, cmdline


def test_reap_stale_daemons_skips_bash_lc_session_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session backend's `bash -lc` wrapper must NEVER be reaped (#1199).

    The wrapper's cmdline CONTAINS the module name (the whole launch command is
    one argv element) and it lives in the project root with our AVA_HOME — the
    old substring match killed it, orphaning the real daemon (PPID=1), reaping
    the session record (has_session → False, `ava stop` loses the daemon, `ava
    status` shows ✗). Only the real `python -m ava._mcps_daemon` process is a
    reap target.
    """
    monkeypatch.setattr(daemon_mod, "_reap_stale_daemons", _ORIG_REAP)

    home = str(settings.general.ava_home)
    root = str(Path(daemon_mod.__file__).resolve().parent.parent)

    class FakeProc:
        def __init__(self, pid: int, cmdline: list[str], cwd: str, env: dict[str, str]) -> None:
            self.pid = pid
            self.info = {"cmdline": cmdline}
            self._cwd = cwd
            self._env = env
            self.killed = False

        def cwd(self) -> str:
            return self._cwd

        def environ(self) -> dict[str, str]:
            return self._env

        def kill(self) -> None:
            self.killed = True

    inner = (
        f"cd {root} && export VIRTUAL_ENV={root}/.venv && "
        f'export PATH={root}/.venv/bin:"$PATH" && .venv/bin/python -m ava._mcps_daemon'
    )
    procs = [
        FakeProc(2001, ["bash", "-lc", inner], root, {"AVA_HOME": home}),  # live session wrapper
        FakeProc(
            2002, [".venv/bin/python", "-m", "ava._mcps_daemon"], root, {"AVA_HOME": home}
        ),  # the real daemon it launched
        FakeProc(2003, ["bash", "-lc", inner], "/other/root", {"AVA_HOME": "/other/home"}),
    ]
    with patch("psutil.process_iter", return_value=procs):
        daemon_mod._reap_stale_daemons(Path(root))

    assert not procs[0].killed and not procs[2].killed  # wrappers survive
    assert procs[1].killed  # the actual daemon is reaped


def test_main_refuses_live_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live socket → exit 1 without entering asyncio.run and without unlink."""
    live = Path(tempfile.gettempdir()) / f"avadaemon_live_{os.getpid()}.sock"
    live.write_text("x")
    try:
        monkeypatch.setattr(sys, "argv", ["_mcps_daemon", str(live)])
        with (
            patch.object(daemon_mod, "_socket_is_live", return_value=True),
            patch.object(daemon_mod.asyncio, "run") as run_spy,
            pytest.raises(SystemExit) as exc,
        ):
            daemon_mod.main()
        assert exc.value.code == 1
        run_spy.assert_not_called()
        # The live guard must not touch the file it refuses to take over.
        assert live.exists()
    finally:
        live.unlink(missing_ok=True)


def test_main_starts_over_stale_socket_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover (dead) socket file does not block startup — only a LIVE one does."""
    monkeypatch.setattr(sys, "argv", ["_mcps_daemon", "/tmp/stale.sock"])  # noqa: S108
    with (
        patch.object(daemon_mod, "_socket_is_live", return_value=False),
        patch.object(daemon_mod.asyncio, "run") as run_spy,
    ):
        daemon_mod.main()
    run_spy.assert_called_once()
    coro = run_spy.call_args.args[0]
    coro.close()


# ─── assert_requirements in connect path ─────────────────────────────────


async def test_connect_server_enforces_requires_before_connecting(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose `requires` is unmet raises BEFORE any stdio launch."""
    _write_config(fake_home, {"chrome": {"command": "npx", "requires": {"display": True}}})
    import ava._mcp_config as _cfg

    # display_available is imported into _mcp_config from shared.platform_probes;
    # patch the bound name (where assert_requirements calls it).
    monkeypatch.setattr(_cfg, "display_available", lambda: False)
    called = False

    def _boom(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)
    with pytest.raises(_cfg.MCPError, match="requires a display"):
        await daemon_mod._connect_server("chrome")
    assert called is False


# ─── _is_transport_error ─────────────────────────────────────────────────


def test_is_transport_error_broken_pipe() -> None:
    assert daemon_mod._is_transport_error(BrokenPipeError()) is True


def test_is_transport_error_connection_reset() -> None:
    assert daemon_mod._is_transport_error(ConnectionResetError()) is True


def test_is_transport_error_timeout_error() -> None:
    assert daemon_mod._is_transport_error(TimeoutError()) is True


def test_is_transport_error_oserror_epipe() -> None:
    assert daemon_mod._is_transport_error(OSError(32, "Broken pipe")) is True


def test_is_transport_error_oserror_econnreset() -> None:
    assert daemon_mod._is_transport_error(OSError(54, "Connection reset by peer")) is True


def test_is_transport_error_oserror_other() -> None:
    """OSError with a non-transport errno (e.g. ENOENT) is not a transport error."""
    assert daemon_mod._is_transport_error(OSError(2, "No such file")) is False


def test_is_transport_error_value_error() -> None:
    assert daemon_mod._is_transport_error(ValueError("not transport")) is False


def test_is_transport_error_runtime_error() -> None:
    assert daemon_mod._is_transport_error(RuntimeError("not transport")) is False


def test_is_transport_error_mcp_error_wraps_transport() -> None:
    """MCPError whose __cause__ is BrokenPipeError is a transport error."""
    from mcp import MCPError

    inner = BrokenPipeError()
    exc = MCPError(-1, "wrapped")
    exc.__cause__ = inner
    assert daemon_mod._is_transport_error(exc) is True


def test_is_transport_error_mcp_error_no_cause() -> None:
    """MCPError without __cause__ is not a transport error."""
    from mcp import MCPError

    assert daemon_mod._is_transport_error(MCPError(-1, "no cause")) is False


def test_is_transport_error_mcp_error_connection_closed_no_cause() -> None:
    """The mcp SDK raises `MCPError(CONNECTION_CLOSED)` `from None` when the
    stdio peer's read loop hits EOF — the __cause__ probe alone missed it, so
    the daemon never rebuilt the dead session (2026-08-13 #1229). The code must
    count as a transport error regardless of __cause__."""
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    assert daemon_mod._is_transport_error(MCPError(CONNECTION_CLOSED, "Connection closed")) is True


def test_is_transport_error_mcp_error_request_timeout_no_cause() -> None:
    """REQUEST_TIMEOUT is the client-side synthesis for a wedged session — same
    treatment as CONNECTION_CLOSED (parity with the browser-mcp daemon)."""
    from mcp import MCPError
    from mcp.types import REQUEST_TIMEOUT

    assert daemon_mod._is_transport_error(MCPError(REQUEST_TIMEOUT, "Timed out")) is True


def test_is_transport_error_mcp_error_tool_code_no_cause() -> None:
    """A server-returned JSON-RPC error (invalid params / unknown tool) must not
    trigger a reconnect — retrying would double-run a side-effectful tool."""
    from mcp import MCPError
    from mcp.types import INVALID_PARAMS

    assert daemon_mod._is_transport_error(MCPError(INVALID_PARAMS, "Bad args")) is False


def test_is_transport_error_anyio_broken_resource() -> None:
    """anyio.BrokenResourceError should be detected by name match."""
    try:
        from anyio import BrokenResourceError
    except ImportError:
        pytest.skip("anyio not installed")
    assert daemon_mod._is_transport_error(BrokenResourceError()) is True


# ─── _invalidate_session ─────────────────────────────────────────────────


async def test_invalidate_session_removes_cached_session_and_closes_stack(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session is removed from both dicts and its stack is aclose()'d."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    stack.aclose = AsyncMock()
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    await daemon_mod._get_session(
        "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert "fs" in daemon_mod.sessions
    assert "fs" in daemon_mod.stacks

    await daemon_mod._invalidate_session(
        "fs", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert "fs" not in daemon_mod.sessions
    assert "fs" not in daemon_mod.stacks
    stack.aclose.assert_awaited_once()


async def test_invalidate_session_noop_when_not_cached() -> None:
    """Invalidating an uncached server does nothing (no error)."""
    await daemon_mod._invalidate_session(
        "nonexistent", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    # no exception raised


# ─── _handle_client retry ─────────────────────────────────────────────────


async def test_handle_client_retries_on_transport_error_and_succeeds(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call_tool raises BrokenPipeError (transport) → retry → second
    attempt succeeds after session invalidation."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    call_count = 0

    async def _flaky_call(*_a: Any, **_k: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BrokenPipeError
        return _call_result(content=[], is_error=False)

    session = _make_session()
    session.call_tool = AsyncMock(side_effect=_flaky_call)
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert call_count == 2


async def test_handle_client_does_not_retry_non_transport_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ValueError is not a transport error → no retry, error propagates."""
    _write_config(fake_home, {"fs": {"command": "x"}})

    call_count = 0

    async def _failing_call(*_a: Any, **_k: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        raise ValueError("bad input")

    session = _make_session()
    session.call_tool = AsyncMock(side_effect=_failing_call)
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is False
    assert "ValueError" in resp["error"]
    assert call_count == 1  # no retry


async def test_handle_client_gives_up_after_max_retries(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After 3 transport errors, error propagates."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    call_count = 0

    async def _always_broken(*_a: Any, **_k: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        raise BrokenPipeError

    session = _make_session()
    session.call_tool = AsyncMock(side_effect=_always_broken)
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is False
    assert "BrokenPipeError" in resp["error"]
    assert call_count == 3  # tried all 3 times


async def test_handle_client_retry_reconnects_after_invalidation(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After transport error invalidates session, _connect_server is called
    again to create a fresh session for the retry."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    connect_count = 0

    async def _reconnect(server: str) -> Any:
        nonlocal connect_count
        connect_count += 1
        session = _make_session()
        if connect_count == 1:
            # First session dies on call
            session.call_tool = AsyncMock(side_effect=BrokenPipeError())
        else:
            session.call_tool = AsyncMock(return_value=_call_result(content=[], is_error=False))
        return session, MagicMock()

    monkeypatch.setattr(daemon_mod, "_connect_server", _reconnect)

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert connect_count == 2  # reconnected after invalidation


async def test_handle_client_retries_on_mcp_error_connection_closed(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (#1229): the SDK raises `MCPError(CONNECTION_CLOSED)` `from
    None` when the stdio server died. The retry loop must invalidate the cached
    dead session, reconnect, and succeed — instead of serving the dead session
    to every later call."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    connect_count = 0

    async def _reconnect(server: str) -> Any:
        nonlocal connect_count
        connect_count += 1
        session = _make_session()
        if connect_count == 1:
            # First session's stdio peer died: the SDK surfaces it as
            # MCPError(CONNECTION_CLOSED), raised `from None` (no __cause__).
            session.call_tool = AsyncMock(
                side_effect=MCPError(CONNECTION_CLOSED, "Connection closed")
            )
        else:
            session.call_tool = AsyncMock(return_value=_call_result(content=[], is_error=False))
        return session, MagicMock()

    monkeypatch.setattr(daemon_mod, "_connect_server", _reconnect)

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert connect_count == 2  # dead session invalidated, fresh one connected


async def test_handle_client_does_not_retry_tool_level_mcp_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server-returned JSON-RPC error (MCPError with INVALID_PARAMS) is not a
    transport death: no invalidate / no retry, so a side-effectful tool is never
    double-run."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    from mcp import MCPError
    from mcp.types import INVALID_PARAMS

    call_count = 0

    async def _failing_call(*_a: Any, **_k: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        raise MCPError(INVALID_PARAMS, "Bad args")

    session = _make_session()
    session.call_tool = AsyncMock(side_effect=_failing_call)
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    [resp] = writer.responses()
    assert resp["ok"] is False
    assert "MCPError" in resp["error"]
    assert call_count == 1  # no retry


async def test_handle_client_retry_sleeps_exponential_backoff(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each retry sleeps with increasing backoff (0s, 1s, 2s)."""
    _write_config(fake_home, {"fs": {"command": "x"}})

    sleeps: list[float] = []

    async def _fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(daemon_mod.asyncio, "sleep", _fake_sleep)

    session = _make_session()
    session.call_tool = AsyncMock(side_effect=BrokenPipeError())
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "x"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )
    # 3 attempts = 2 retries = 2 sleep calls: attempt 0 fails → sleep(1),
    # attempt 1 fails → sleep(2), attempt 2 fails → no sleep (last attempt)
    assert sleeps == [1, 2]


# ─── shared daemon: per-connection session isolation ─────────────────────


async def test_shared_connections_isolate_sessions(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two clients on the shared daemon never share session state.

    Each connection gets its own sessions/stacks/locks (created inside
    `_handle_connection`); agent A's `fs` session must be invisible to agent B,
    and each connection triggers its own `_connect_server`.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session_a = _make_session()
    session_b = _make_session()
    connect = AsyncMock(side_effect=[(session_a, MagicMock()), (session_b, MagicMock())])
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _call_list_tools() -> tuple[list[dict[str, Any]], Any]:
        """Run one full connection (request line -> response) with fresh state."""
        req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer))
        return writer.responses(), writer

    resp_a, _ = await _call_list_tools()
    resp_b, _ = await _call_list_tools()
    assert resp_a[0]["ok"] is True and resp_b[0]["ok"] is True
    # two independent connects — one per connection
    assert connect.await_count == 2
    # module-level caches stay untouched by the shared path
    assert daemon_mod.sessions == {}


async def test_handle_connection_cleans_sessions_on_close(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a client connection ends, its MCP server subprocesses are released.

    The whole point of per-connection ownership: a dead agent must not leak its
    chrome/x stdio children in the shared daemon.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    aclose = AsyncMock()
    stack.aclose = aclose  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode(), b""])
    writer = _FakeWriter()
    await daemon_mod._handle_connection(reader, _writer_arg(writer))
    # the connection's own stack was closed on disconnect
    aclose.assert_awaited_once()


async def test_handle_client_ping_returns_pong() -> None:
    """Lock-free liveness probe: no config / session involved, answers pong.

    The watchdog healthcheck dials the shared socket with ping; a slow MCP
    server must never make the daemon look dead.
    """
    req = {"id": 0, "method": "ping"}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(reader, _writer_arg(writer), {}, {}, {})
    [resp] = writer.responses()
    assert resp == {"id": 0, "ok": True, "result": "pong"}


# ─── server subprocess sharing (`shared` spec) ────────────────────────────


async def test_get_session_shared_true_uses_daemon_wide_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": true` server caches in the daemon-wide buckets and is
    wrapped in a serializing session — one stdio child for every connection."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session()
    stack = MagicMock()
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    got = await daemon_mod._get_session(
        "disc", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert isinstance(got, daemon_mod._SerialSession)
    # per-connection buckets untouched; daemon-wide buckets hold the child
    assert daemon_mod.sessions == {}
    assert daemon_mod.shared_sessions["disc"] is got
    assert daemon_mod.shared_stacks["disc"] is stack


async def test_shared_connections_share_one_server_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two client connections on a shared server trigger exactly one connect:
    the daemon-wide child is reused, and closing one connection does not
    release it (it outlives every connection, released at daemon shutdown)."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session(tools=[_tool("echo")])
    stack = MagicMock()
    stack.aclose = AsyncMock()  # type: ignore[method-assign]
    connect = AsyncMock(return_value=(session, stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _one_connection() -> None:
        req = {"id": 1, "method": "list_tools", "params": {"server": "disc"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer))

    await _one_connection()
    await _one_connection()

    assert connect.await_count == 1  # one child for two connections
    # connection teardown did not release the shared child
    assert daemon_mod.shared_sessions["disc"] is not None
    stack.aclose.assert_not_awaited()


async def test_shared_browser_server_connects_direct_no_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": "browser"` server is dialed directly (no stdio child):
    _connect_server dispatches to connect_browser_direct, never stdio_client."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    browser_session = MagicMock()
    browser_stack = MagicMock()
    direct = AsyncMock(return_value=(browser_session, browser_stack))
    # _connect_server imports the direct-connect helper lazily inside the
    # function, so patch the source module attribute it resolves.
    import ava._mcp_browser as browser_mod

    monkeypatch.setattr(browser_mod, "connect_browser_direct", direct)
    spawned: list[int] = []

    def _boom(*_a: object, **_k: object) -> None:
        spawned.append(1)

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)

    session, stack = await daemon_mod._connect_server("chrome")
    assert session is browser_session and stack is browser_stack
    direct.assert_awaited_once_with()
    assert spawned == []


async def test_get_session_shared_browser_uses_per_connection_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": "browser"` server caches in the CALLER's per-connection
    buckets, not the daemon-wide ones: each agent connection keeps its own
    socket, so the browser-mcp service's page affinity stays per agent and no
    connection can corrupt another's request-id stream. It is not wrapped in
    _SerialSession either — one connection is one serial caller by design."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    browser_session = MagicMock()
    browser_stack = MagicMock()
    connect = AsyncMock(return_value=(browser_session, browser_stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    got = await daemon_mod._get_session(
        "chrome", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert got is browser_session
    assert daemon_mod.sessions["chrome"] is browser_session
    assert daemon_mod.stacks["chrome"] is browser_stack
    # daemon-wide buckets untouched — the socket dies with its connection
    assert daemon_mod.shared_sessions == {}
    assert daemon_mod.shared_stacks == {}


async def test_shared_browser_connections_keep_own_socket(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two client connections on `"shared": "browser"` each dial their own
    browser-mcp socket (one connect per connection) and release it on close —
    there is no daemon-wide shared browser socket to desync."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    connect = AsyncMock(
        side_effect=[(_make_session(), MagicMock()), (_make_session(), MagicMock())]
    )
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _one_connection() -> None:
        req = {"id": 1, "method": "list_tools", "params": {"server": "chrome"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer))

    await _one_connection()
    await _one_connection()
    assert connect.await_count == 2  # per-connection sockets, no sharing
    assert daemon_mod.shared_sessions == {}


@pytest.mark.flaky  # real AF_UNIX sockets: two live browser-service connections
async def test_browser_concurrent_connections_no_id_desync(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections calling the browser server concurrently both succeed.

    Regression for the id-desync bug (browser-mcp daemon response id N !=
    request N+2, permanently): the browser session used to live in the
    daemon-wide buckets, so concurrent connections multiplexed on ONE socket
    with ONE shared id counter and corrupted each other's response stream.
    Each connection must dial its own socket."""
    import ava._mcp_browser as browser_mod

    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})

    sock_path = Path(
        f"/tmp/ava-browser-{os.getpid()}-{os.urandom(3).hex()}.sock"  # noqa: S108 — test-only short AF_UNIX path
    )
    with contextlib.suppress(OSError):
        sock_path.unlink()

    async def _browser_service(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        # Serial, echoes request ids; the delay makes the two clients overlap
        # (both in flight before either response arrives).
        try:
            while line := await r.readline():
                await asyncio.sleep(0.05)
                req = json.loads(line)
                resp = {"id": req["id"], "ok": True, "result": {"content": [], "isError": False}}
                w.write((json.dumps(resp) + "\n").encode())
                await w.drain()
        finally:
            w.close()

    server = await asyncio.start_unix_server(_browser_service, path=str(sock_path))

    async def _direct() -> tuple[Any, Any]:
        reader, writer = await asyncio.open_unix_connection(sock_path, limit=64 * 1024 * 1024)
        session = browser_mod.BrowserLineSession(reader, writer, sock=str(sock_path))
        stack = contextlib.AsyncExitStack()
        stack.push_async_callback(session.close)
        return session, stack

    monkeypatch.setattr(browser_mod, "connect_browser_direct", _direct)

    async def _one_connection(req_id: int) -> list[dict[str, Any]]:
        req = {
            "id": req_id,
            "method": "call_tool",
            "params": {"server": "chrome", "tool": "navigate", "args": {"url": "https://x"}},
        }
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer))
        return writer.responses()

    try:
        resp_a, resp_b = await asyncio.gather(_one_connection(1), _one_connection(2))
        assert resp_a[0]["ok"] is True
        assert resp_b[0]["ok"] is True
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock_path.unlink()


async def test_connect_server_unknown_shared_value_raises(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized `shared` value fails fast instead of silently falling
    back to a per-connection child (which would defeat the declared intent)."""
    _write_config(fake_home, {"weird": {"command": "x", "shared": "mars"}})
    with pytest.raises(ValueError, match="unknown shared value 'mars'"):
        await daemon_mod._connect_server("weird")


async def test_invalidate_session_shared_clears_daemon_wide_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport-error invalidation of a shared server rebuilds the daemon-wide
    child (not the caller's per-connection buckets)."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session()
    stack = MagicMock()
    stack.aclose = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))
    await daemon_mod._get_session(
        "disc", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert "disc" in daemon_mod.shared_sessions

    await daemon_mod._invalidate_session(
        "disc", daemon_mod.sessions, daemon_mod.stacks, daemon_mod.session_locks
    )
    assert "disc" not in daemon_mod.shared_sessions
    assert "disc" not in daemon_mod.shared_stacks
    stack.aclose.assert_awaited_once()


async def test_serial_session_serializes_concurrent_calls() -> None:
    """Concurrent calls on a shared session are serialized under one lock: the
    inner session sees one in-flight call at a time."""
    inner = _make_session()
    entered = 0
    max_inflight = 0
    in_flight = 0
    release = asyncio.Event()

    async def _slow_list_tools() -> MagicMock:
        nonlocal entered, max_inflight, in_flight
        entered += 1
        in_flight += 1
        max_inflight = max(max_inflight, in_flight)
        await release.wait()
        in_flight -= 1
        return MagicMock(tools=[])

    inner.list_tools = AsyncMock(side_effect=_slow_list_tools)  # type: ignore[method-assign]
    serial = daemon_mod._SerialSession(inner, asyncio.Lock())

    t1 = asyncio.create_task(serial.list_tools())
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(serial.list_tools())
    await asyncio.sleep(0.01)
    assert entered == 1  # second call is blocked on the lock
    release.set()
    await asyncio.gather(t1, t2)
    assert entered == 2
    assert max_inflight == 1  # never two at once


def test_shared_kind_reads_spec(fake_home: Path) -> None:
    """_shared_kind returns the declared value, None when absent/unknown."""
    _write_config(
        fake_home,
        {
            "a": {"command": "x", "shared": True},
            "b": {"command": "x", "shared": "browser"},
            "c": {"command": "x"},
        },
    )
    assert daemon_mod._shared_kind("a") is True
    assert daemon_mod._shared_kind("b") == "browser"
    assert daemon_mod._shared_kind("c") is None
    assert daemon_mod._shared_kind("nope") is None


# ─── _connect_server / _connect_http: remote (url) servers ───────────────


async def test_connect_server_routes_url_servers_to_http(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `url` entry skips the stdio child path entirely — `_connect_http` owns it."""
    _write_config(fake_home, {"remote": {"url": "https://mcp.example.com/mcp"}})
    http = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr(daemon_mod, "_connect_http", http)

    session, stack = await daemon_mod._connect_server("remote")

    http.assert_awaited_once_with("https://mcp.example.com/mcp", None, oauth=False, server="remote")
    assert session is http.return_value[0]
    assert stack is http.return_value[1]


async def test_connect_http_initializes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP connect = streamable_http_client -> ClientSession -> initialize."""
    read, write = object(), object()
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(read, write))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", session_cls)

    got, stack = await daemon_mod._connect_http("https://mcp.example.com/mcp", None)

    assert got is session
    session.initialize.assert_awaited_once()
    assert session_cls.call_args.args == (read, write)
    assert factory.call_args.args == ("https://mcp.example.com/mcp",)
    assert factory.call_args.kwargs["http_client"] is None
    await stack.aclose()


async def test_connect_http_passes_headers_to_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static auth headers ride on an SDK-built httpx client."""
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(object(), object()))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.ClientSession", MagicMock(return_value=session_cm))
    client_factory = MagicMock(return_value=object())
    monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", client_factory)

    await daemon_mod._connect_http("https://mcp.example.com/mcp", {"Authorization": "Bearer k"})

    client_factory.assert_called_once_with(headers={"Authorization": "Bearer k"})
    assert factory.call_args.kwargs["http_client"] is client_factory.return_value


async def test_connect_http_fails_fast_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead endpoint raises (no retry) and releases the half-built stack."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=ConnectionError("endpoint unreachable"))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        MagicMock(return_value=_boom()),
    )
    aclose = AsyncMock()
    monkeypatch.setattr(daemon_mod.AsyncExitStack, "aclose", aclose)

    with pytest.raises(ConnectionError):
        await daemon_mod._connect_http("https://mcp.example.com/mcp", None)
    aclose.assert_awaited_once()


async def test_connect_server_routes_oauth_servers(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `oauth: true` url entry builds the OAuth client, not static headers."""
    _write_config(fake_home, {"remote": {"url": "https://mcp.example.com/mcp", "oauth": True}})
    http = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr(daemon_mod, "_connect_http", http)

    await daemon_mod._connect_server("remote")

    http.assert_awaited_once_with("https://mcp.example.com/mcp", None, oauth=True, server="remote")


async def test_connect_http_oauth_builds_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oauth=True hands the HTTP client off to the OAuth builder."""
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(object(), object()))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.ClientSession", MagicMock(return_value=session_cm))

    oauth_client = MagicMock()
    oauth_builder = AsyncMock(return_value=oauth_client)
    import ava._mcp_oauth as oauth_mod

    monkeypatch.setattr(oauth_mod, "oauth_http_client", oauth_builder)

    await daemon_mod._connect_http("https://mcp.example.com/mcp", None, oauth=True, server="exa")

    oauth_builder.assert_awaited_once_with("https://mcp.example.com/mcp", "exa")
    assert factory.call_args.kwargs["http_client"] is oauth_client


async def test_shared_computer_server_connects_direct_no_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": "computer"` server is dialed directly (no stdio child):
    _connect_server dispatches to connect_computer_direct, never stdio_client."""
    _write_config(fake_home, {"computer": {"command": ".venv/bin/python", "shared": "computer"}})
    session = MagicMock()
    stack = MagicMock()
    direct = AsyncMock(return_value=(session, stack))
    import ava._mcp_computer as computer_mod

    monkeypatch.setattr(computer_mod, "connect_computer_direct", direct)
    spawned: list[int] = []

    def _boom(*_a: object, **_k: object) -> None:
        spawned.append(1)

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)

    got_session, got_stack = await daemon_mod._connect_server("computer")
    assert got_session is session and got_stack is stack
    direct.assert_awaited_once_with()
    assert spawned == []


async def test_computer_call_stamps_agent_id(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent id from the client envelope is stamped onto the computer
    session before every call_tool, so the computer daemon can gate and audit
    per agent (the line payload carries it)."""
    _write_config(fake_home, {"computer": {"command": ".venv/bin/python", "shared": "computer"}})
    import ava._mcp_computer as computer_mod

    received: dict[str, object] = {}

    async def _computer_service(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        line = await r.readline()
        req = json.loads(line)
        received.update(req)
        resp = {"id": req["id"], "ok": True, "result": {"content": [], "isError": False}}
        w.write((json.dumps(resp) + "\n").encode())
        await w.drain()
        w.close()

    sock = Path(f"/tmp/ava-computer-{os.getpid()}-{os.urandom(3).hex()}.sock")  # noqa: S108
    with contextlib.suppress(OSError):
        sock.unlink()
    server = await asyncio.start_unix_server(_computer_service, path=str(sock))

    async def _direct() -> tuple[Any, Any]:
        reader, writer = await asyncio.open_unix_connection(str(sock), limit=64 * 1024 * 1024)
        session = computer_mod.ComputerLineSession(reader, writer, sock=str(sock))
        stack = contextlib.AsyncExitStack()
        stack.push_async_callback(session.close)
        return session, stack

    monkeypatch.setattr(computer_mod, "connect_computer_direct", _direct)

    req = {
        "id": 1,
        "method": "call_tool",
        "params": {"server": "computer", "tool": "click", "args": {"x": 1}},
        "agent_id": 42,
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    try:
        await daemon_mod._handle_connection(reader, _writer_arg(writer))
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock.unlink()
    assert received.get("agent_id") == 42
    assert received.get("tool") == "click"


async def test_handle_client_stamps_agent_id_on_session(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's per-request agent_id reaches the session before the call —
    BrowserLineSession (and ComputerLineSession) carry it on the wire so the
    service can key per-agent state. Sessions without the attribute (plain
    stdio servers) are skipped."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result([_content({"type": "text", "text": "hi"})]),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {}},
        "agent_id": 42,
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    session.call_tool.assert_awaited_once_with("read", {})
    assert session.client_agent_id == 42


async def test_handle_client_agent_id_none_without_identity(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request without an agent_id stamps None — the service falls back to
    per-connection affinity for identity-less clients."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result([_content({"type": "text", "text": "hi"})]),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {}},
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        daemon_mod.sessions,
        daemon_mod.stacks,
        daemon_mod.session_locks,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    assert session.client_agent_id is None
