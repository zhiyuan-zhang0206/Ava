"""Daemon cases: run daemon clears stale socket on startup."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ava.mcps._daemon as daemon_mod
from ava.mcps.tests.test_daemon import (
    _ORIG_REAP,
    _FakeProc,
    _FakeWriter,
    _is_daemon_cmdline_cases,
    _make_reader,
    _make_session,
    _wait_for_daemon_socket,
    _write_config,
    _writer_arg,
)
from ava.mcps.tests.test_daemon import (
    _no_reap_stale_daemons as _no_reap_stale_daemons,
)
from ava.mcps.tests.test_daemon import (
    daemon_wide as daemon_wide,
)
from ava.mcps.tests.test_daemon import (
    fake_home as fake_home,
)
from ava.mcps.tests.test_daemon import (
    scope as scope,
)
from ava.mcps.tests.test_daemon import (
    short_socket_path as short_socket_path,
)
from base.host.env.dotenv_boot import resolve_ava_home


@pytest.mark.flaky  # real AF_UNIX socket lifecycle: await-until-ready poll
async def test_run_daemon_clears_stale_socket_on_startup(
    short_socket_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing socket path (leftover from last crash) → run_daemon unlinks on startup then binds."""
    Path(short_socket_path).write_text("leftover")

    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock())
    daemon_task = asyncio.create_task(
        daemon_mod.run_daemon(short_socket_path, timeout_seconds=lambda: 15.0)
    )

    # being able to connect means bind succeeded = stale was cleaned
    _, writer = await _wait_for_daemon_socket(daemon_task, short_socket_path, timeout=1.5)
    writer.close()

    daemon_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await daemon_task


def test_main_requires_socket_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than one arg → exit 1, does not enter asyncio.run.

    Zero args is now the NORMAL shared-daemon mode (binds the per-machine
    socket); only an impossible argv (2+ positional) fails fast.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["_daemon", "/tmp/a.sock", "/tmp/b.sock"],  # noqa: S108
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
    monkeypatch.setattr(sys, "argv", ["_daemon"])
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
    monkeypatch.setattr(sys, "argv", ["_daemon", "/tmp/test.sock"])  # noqa: S108 — fake argv, not actually opened
    with patch.object(daemon_mod.asyncio, "run") as run_spy:
        # asyncio.run accepts coroutine — after patching it doesn't actually run (saves daemon startup overhead)
        run_spy.return_value = None
        daemon_mod.main()
    run_spy.assert_called_once()
    # verify the passed coroutine is run_daemon — assert by name, avoid depending on coroutine identity
    coro = run_spy.call_args.args[0]
    assert coro.__name__ == "run_daemon"
    coro.close()  # close the coroutine to avoid "never awaited" warning


async def test_socket_is_live_true_when_ping_answered(short_socket_path: str) -> None:
    """A daemon answering the ping protocol = live; a second daemon must not start."""

    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(b'{"ok": true}\n')
            await writer.drain()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    sock = short_socket_path
    server = await asyncio.start_unix_server(handler, path=sock)
    try:
        # `_socket_is_live` is a blocking sync socket call — run it off the
        # event loop so the server's handler can be scheduled (the production
        # call site, `main()`, is sync and has no such conflict).
        assert await asyncio.to_thread(daemon_mod._socket_is_live, sock) is True
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            Path(sock).unlink()


async def test_socket_is_live_false_when_connect_but_no_reply(short_socket_path: str) -> None:
    """A socket that accepts but never answers is NOT live — a half-dead occupant
    must be replaceable, not shielded forever by a successful connect."""

    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    sock = short_socket_path
    server = await asyncio.start_unix_server(handler, path=sock)
    try:
        assert await asyncio.to_thread(daemon_mod._socket_is_live, sock) is False
    finally:
        release.set()
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

    home = str(resolve_ava_home())
    root = str(Path(daemon_mod.__file__).resolve().parents[2])

    argv = [".venv/bin/python", "-m", "ava.mcps._daemon"]
    procs = [
        _FakeProc(os.getpid(), argv, root, {"AVA_HOME": home}),  # self
        _FakeProc(1001, argv, root, {}),  # same unit via cwd
        _FakeProc(1002, argv, "/elsewhere", {"AVA_HOME": home}),  # same via env
        _FakeProc(1003, argv, "/other/root", {"AVA_HOME": "/other/home"}),  # other unit
        _FakeProc(1004, ["python", "-m", "something.else"], root, {"AVA_HOME": home}),  # no daemon
    ]
    with patch("psutil.process_iter", return_value=procs):
        daemon_mod._reap_stale_daemons(Path(root))

    assert procs[1].killed and procs[2].killed
    assert not procs[0].killed and not procs[3].killed and not procs[4].killed


def test_is_daemon_cmdline_discriminates_wrappers() -> None:
    """Only argv shaped `python -m ava.mcps._daemon` is a daemon launch."""
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
    status` shows ✗). Only the real `python -m ava.mcps._daemon` process is a
    reap target.
    """
    monkeypatch.setattr(daemon_mod, "_reap_stale_daemons", _ORIG_REAP)

    home = str(resolve_ava_home())
    root = str(Path(daemon_mod.__file__).resolve().parents[2])

    inner = (
        f"cd {root} && export VIRTUAL_ENV={root}/.venv && "
        f'export PATH={root}/.venv/bin:"$PATH" && .venv/bin/python -m ava.mcps._daemon'
    )
    procs = [
        _FakeProc(2001, ["bash", "-lc", inner], root, {"AVA_HOME": home}),  # live session wrapper
        _FakeProc(
            2002, [".venv/bin/python", "-m", "ava.mcps._daemon"], root, {"AVA_HOME": home}
        ),  # the real daemon it launched
        _FakeProc(2003, ["bash", "-lc", inner], "/other/root", {"AVA_HOME": "/other/home"}),
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
        monkeypatch.setattr(sys, "argv", ["_daemon", str(live)])
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
    monkeypatch.setattr(sys, "argv", ["_daemon", "/tmp/stale.sock"])  # noqa: S108
    with (
        patch.object(daemon_mod, "_socket_is_live", return_value=False),
        patch.object(daemon_mod.asyncio, "run") as run_spy,
    ):
        daemon_mod.main()
    run_spy.assert_called_once()
    coro = run_spy.call_args.args[0]
    coro.close()


async def test_connect_server_enforces_requires_before_connecting(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose `requires` is unmet raises BEFORE any stdio launch."""
    _write_config(fake_home, {"chrome": {"command": "npx", "requires": {"display": True}}})
    import ava.mcp_config as _cfg

    # display_available is imported into mcp_config from base.host.system.probes;
    # patch the bound name (where assert_requirements calls it).
    monkeypatch.setattr(_cfg, "display_available", lambda: False)
    called = False

    def _boom(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)
    with pytest.raises(_cfg.MCPError, match="requires a display"):
        await daemon_mod._connect_server("chrome", {}, timeout_seconds=lambda: 15.0)
    assert called is False


@pytest.mark.parametrize(
    ("overlay", "error"),
    [
        ("{not json", "mcp_enabled.json"),
        ('{"mcp_servers":{"fs":{"enabled":false}}}', "not configured"),
    ],
)
async def test_overlay_error_or_disabled_server_never_starts_a_process(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: daemon_mod._Scope,
    overlay: str,
    error: str,
) -> None:
    import mcp.client.stdio

    _write_config(fake_home, {"fs": {"command": "must-not-run"}})
    (fake_home / "mcp_enabled.json").write_text(overlay)
    launch = MagicMock(side_effect=AssertionError("server startup must not be reached"))
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", launch)
    response = await daemon_mod._dispatch_with_retry(
        {"id": 1, "method": "list_tools", "params": {"server": "fs"}}, scope
    )
    assert response["ok"] is False
    assert error in response["error"]
    launch.assert_not_called()
    assert scope.local.sessions == {} and scope.shared.sessions == {}
    assert await daemon_mod._handle_ping(2) == {"id": 2, "ok": True, "result": "pong"}


def test_is_transport_error_broken_pipe() -> None:
    assert daemon_mod._is_transport_error(BrokenPipeError()) is True


def test_is_transport_error_connection_reset() -> None:
    assert daemon_mod._is_transport_error(ConnectionResetError()) is True


def test_is_transport_error_timeout_error() -> None:
    assert daemon_mod._is_transport_error(TimeoutError()) is True


def test_is_transport_error_oserror_epipe() -> None:
    assert daemon_mod._is_transport_error(OSError(32, "Broken pipe")) is True


def test_is_transport_error_oserror_econnreset() -> None:
    assert (
        daemon_mod._is_transport_error(OSError(errno.ECONNRESET, "Connection reset by peer"))
        is True
    )


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
    """The required transport dependency supplies the concrete exception type."""
    from anyio import BrokenResourceError

    assert daemon_mod._is_transport_error(BrokenResourceError()) is True


async def test_invalidate_session_removes_cached_session_and_closes_stack(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """Session is removed from both dicts and its stack is aclose()'d."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    stack.aclose = AsyncMock()
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    await daemon_mod._get_session("fs", scope)
    assert "fs" in scope.local.sessions
    assert "fs" in scope.local.stacks

    await daemon_mod._invalidate_session("fs", scope)
    assert "fs" not in scope.local.sessions
    assert "fs" not in scope.local.stacks
    stack.aclose.assert_awaited_once()


async def test_invalidate_session_noop_when_not_cached(scope: daemon_mod._Scope) -> None:
    """Invalidating an uncached server does nothing (no error)."""
    await daemon_mod._invalidate_session("nonexistent", scope)


async def test_handle_client_retries_on_transport_error_and_succeeds(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """Safe tool listing still retries after a transport failure."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    call_count = 0

    async def _flaky_call(*_a: Any, **_k: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BrokenPipeError
        return MagicMock(tools=[])

    session = _make_session()
    session.list_tools = AsyncMock(side_effect=_flaky_call)
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
    assert resp["ok"] is True
    assert call_count == 2


async def test_handle_client_does_not_retry_non_transport_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
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
        scope,
    )
    [resp] = writer.responses()
    assert resp["ok"] is False
    assert "ValueError" in resp["error"]
    assert call_count == 1  # no retry


async def test_handle_client_gives_up_after_max_retries(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
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
    session.list_tools = AsyncMock(side_effect=_always_broken)
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
    assert resp["ok"] is False
    assert "BrokenPipeError" in resp["error"]
    assert call_count == 3  # tried all 3 times


async def test_handle_client_retry_reconnects_after_invalidation(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """A safe list_tools retry rebuilds the dead session."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    connect_count = 0

    async def _reconnect(server: str, _oauth_locks: dict[str, Any], **_kwargs: Any) -> Any:
        nonlocal connect_count
        connect_count += 1
        session = _make_session()
        if connect_count == 1:
            session.list_tools = AsyncMock(side_effect=BrokenPipeError())
        else:
            session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        return session, MagicMock()

    monkeypatch.setattr(daemon_mod, "_connect_server", _reconnect)

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert connect_count == 2  # reconnected after invalidation


async def test_handle_client_retries_on_mcp_error_connection_closed(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """A listing retries SDK CONNECTION_CLOSED after rebuilding its session."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", AsyncMock())  # skip real retry backoff

    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    connect_count = 0

    async def _reconnect(server: str, _oauth_locks: dict[str, Any], **_kwargs: Any) -> Any:
        nonlocal connect_count
        connect_count += 1
        session = _make_session()
        if connect_count == 1:
            # First session's stdio peer died: the SDK surfaces it as
            # MCPError(CONNECTION_CLOSED), raised `from None` (no __cause__).
            session.list_tools = AsyncMock(
                side_effect=MCPError(CONNECTION_CLOSED, "Connection closed")
            )
        else:
            session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        return session, MagicMock()

    monkeypatch.setattr(daemon_mod, "_connect_server", _reconnect)

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    [resp] = writer.responses()
    assert resp["ok"] is True
    assert connect_count == 2  # dead session invalidated, fresh one connected


async def test_handle_client_does_not_retry_tool_level_mcp_error(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
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
        scope,
    )
    [resp] = writer.responses()
    assert resp["ok"] is False
    assert "MCPError" in resp["error"]
    assert call_count == 1  # no retry
