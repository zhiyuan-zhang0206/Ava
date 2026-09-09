"""SIGTERM shutdown tests for the shared chrome MCP daemon (#2043).

The 2026-09-09 `cluster update` outage hung because the daemon received
SIGTERM but never exited: the finally path awaited the cancelled session
task under ``suppress(Exception)`` (CancelledError is a BaseException, so it
escaped and skipped the upstream stack / server / socket cleanup) and several
cleanup awaits were unbounded. These tests run the real daemon as a private
subprocess with a fake ``npx`` upstream and a fake Chrome listener, and prove:

- TERM on a normally connected daemon exits it and closes server, socket and
  the upstream child, without touching the independently-retained Chrome.
- TERM while the upstream connect hangs interrupts the connect immediately.
- TERM during reconnect churn exits promptly with no surviving children.
- A cancelled session task never skips the remaining cleanup (in-process).
- A wedged cleanup await is bounded, so shutdown always completes.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil
import pytest


# The daemon's Unix socket lives under $AVA_HOME/run; pytest's tmp_path on
# macOS sits under /private/var/folders/... and breaks the 104-byte AF_UNIX
# path limit, which makes the daemon's single-instance probe fail closed and
# exit silently. A short home under /tmp keeps every path inside the limit.
@pytest.fixture
def home(tmp_path: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix="ava-mcp-home-", dir="/tmp"))


_FAKE_UPSTREAM = r"""
import json, os, signal, sys

def _die(*_args: object) -> None:
    os._exit(0)

signal.signal(signal.SIGTERM, _die)
signal.signal(signal.SIGINT, _die)

mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FAKE_UPSTREAM_MODE", "ok")
if mode == "hang-initialize":
    while True:  # never answer; exit only on TERM/EOF
        if not sys.stdin.readline():
            os._exit(0)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"protocolVersion": proto, "capabilities": {},
                       "serverInfo": {"name": "fake", "version": "0"}},
        }) + "\n")
        sys.stdout.flush()
    elif method in ("ping", "tools/list", "tools/call"):
        if method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()
        if mode == "die-after-init" and method == "initialize":
            os._exit(0)  # answer the handshake, then die like a crashed upstream
os._exit(0)
"""


class _ChromeFake:
    """A listening socket standing in for the independently-retained Chrome.

    Counts accepted connections: the daemon never dials the CDP port itself
    (only the upstream does), so a normal stop must leave this untouched.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        with suppress(OSError):
            self._sock.close()


def _fake_bin(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    """A PATH dir whose ``npx`` execs the fake upstream in the given mode.

    The mode is baked into the script: the mcp SDK's stdio environment does
    not pass unknown vars through to the child, so an env-only mode would
    silently fall back to "ok".
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    upstream = tmp_path / "fake_upstream.py"
    upstream.write_text(_FAKE_UPSTREAM, encoding="utf-8")
    npx = bin_dir / "npx"
    npx.write_text(f'#!/bin/sh\nexec {sys.executable} {upstream} {mode} "$@"\n', encoding="utf-8")
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    return bin_dir, upstream


def _daemon_env(home: Path, bin_dir: Path, chrome: _ChromeFake, mode: str) -> dict[str, str]:
    # browser_cdp_port is cluster-pinned: _enforce_cluster_env_authority drops it
    # from the environment unless the unit's own .env declares it. Write a real
    # unit .env in the private home — the same source a production unit uses.
    # The connect timeout is pinned long so the hung-connect test proves the
    # stop event beats the timeout rather than the other way around.
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"AVA_BROWSER_CDP_PORT={chrome.port}\nAVA_MCP_CONNECT_TIMEOUT_SECONDS=120\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(
        {
            "AVA_HOME": str(home),
            "AVA_HOME_OVERRIDE": "1",
            "FAKE_UPSTREAM_MODE": mode,
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    # The suite runs under AVA_RUNNER_MODE=hosted: a hosted runner's Settings
    # build fetches config from AVA_GATEWAY_URL (a test stub here), which would
    # kill the daemon before it binds its socket. A plain private unit reads its
    # own .env only — exactly the production single-box/standalone shape.
    for key in (
        "AVA_RUNNER_MODE",
        "AVA_CONFIG_FETCH",
        "AVA_GATEWAY_URL",
        "AVA_PRIMARY_GATEWAY_URL",
        "AVA_CLUSTER",
    ):
        env.pop(key, None)
    return env


def _socket_path(home: Path, chrome: _ChromeFake) -> Path:
    return home / "run" / f"chrome-mcp.{chrome.port}.sock"


def _spawn(
    home: Path, tmp_path: Path, bin_dir: Path, chrome: _ChromeFake, mode: str
) -> tuple[subprocess.Popen[bytes], Path]:
    """Start the daemon; return (process, its output log path)."""
    log_path = tmp_path / "daemon.log"
    env = _daemon_env(home, bin_dir, chrome, mode)
    (tmp_path / "daemon.env").write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(env.items())), encoding="utf-8"
    )
    log = log_path.open("wb")
    return (
        subprocess.Popen(
            [sys.executable, "-u", "-m", "services.browser.mcp_daemon"],
            env=env,
            cwd=Path(__file__).resolve().parents[2],
            stdout=log,
            stderr=subprocess.STDOUT,
        ),
        log_path,
    )


def _wait_socket(
    path: Path, daemon: subprocess.Popen[bytes], log_path: Path, timeout: float = 20.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if daemon.poll() is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"daemon exited early with rc={daemon.returncode}:\n{log[-3000:]}")
        time.sleep(0.05)
    raise AssertionError(f"daemon socket {path} did not appear within {timeout}s")


def _client_roundtrip(path: Path, request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall((json.dumps(request) + "\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode())


def _client_roundtrip_until_ok(
    path: Path, request: dict[str, object], timeout: float = 30.0
) -> dict[str, object]:
    """The daemon answers transient errors while its upstream connects; retry
    until the round-trip succeeds (the real bridges retry the same way)."""
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _client_roundtrip(path, request)
        if last.get("ok") is True:
            return last
        time.sleep(0.2)
    raise AssertionError(f"round-trip never succeeded: {last}")


def _upstream_child_pid(daemon: subprocess.Popen[bytes]) -> int:
    try:
        children = psutil.Process(daemon.pid).children(recursive=False)
    except psutil.NoSuchProcess:
        return 0
    return children[0].pid if children else 0


def _term_and_wait(daemon: subprocess.Popen[bytes], log_path: Path, timeout: float = 25.0) -> int:
    with suppress(ProcessLookupError):
        os.kill(daemon.pid, signal.SIGTERM)
    try:
        return daemon.wait(timeout=timeout)
    except subprocess.TimeoutExpired as err:
        daemon.kill()
        daemon.wait(timeout=5)
        raise AssertionError(
            f"daemon did not exit within {timeout}s after SIGTERM; log:\n"
            f"{log_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        ) from err


@pytest.mark.flaky
def test_sigterm_normal_connection_closes_everything_except_chrome(
    home: Path, tmp_path: Path
) -> None:
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "ok")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "ok")
    sock_path = _socket_path(home, chrome)
    try:
        _wait_socket(sock_path, daemon, daemon_log)
        # A real round-trip proves the upstream is connected (list_tools goes
        # through the fake npx subprocess).
        resp = _client_roundtrip_until_ok(sock_path, {"id": 1, "method": "list_tools"})
        assert resp["result"] == []

        child_pid = _upstream_child_pid(daemon)
        assert child_pid, "no upstream child — the fake npx did not spawn"

        rc = _term_and_wait(daemon, daemon_log)
        assert rc == 0
        assert not sock_path.exists(), "daemon socket must be unlinked on stop"
        assert not psutil.pid_exists(child_pid), "upstream child must exit with the daemon"
        assert chrome.accepted == 0, "stop must never touch the retained Chrome"
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


@pytest.mark.flaky
def test_sigterm_during_hung_connect_interrupts_immediately(home: Path, tmp_path: Path) -> None:
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "hang-initialize")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "hang-initialize")
    sock_path = _socket_path(home, chrome)
    try:
        _wait_socket(sock_path, daemon, daemon_log)
        time.sleep(0.5)  # the daemon is now inside session.initialize()
        start = time.monotonic()
        rc = _term_and_wait(daemon, daemon_log)
        # Connect timeout is 120s; the stop event must win the race far sooner.
        assert rc == 0
        assert time.monotonic() - start < 15, "TERM must interrupt a hung connect"
        assert not sock_path.exists()
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


@pytest.mark.flaky
def test_sigterm_during_reconnect_churn_leaves_no_children(home: Path, tmp_path: Path) -> None:
    """TERM while the daemon flaps between dead-upstream cleanup and reconnect.

    The fake upstream answers initialize and then exits immediately, so the
    daemon is permanently inside the death-cleanup / backoff / reconnect path
    (upstream child already gone, SDK cleanup still pending) when TERM lands.
    """
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "die-after-init")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "die-after-init")
    sock_path = _socket_path(home, chrome)
    try:
        _wait_socket(sock_path, daemon, daemon_log)
        time.sleep(1.5)  # a few connect→death→cleanup cycles have run
        rc = _term_and_wait(daemon, daemon_log)
        assert rc == 0
        assert not sock_path.exists()
        try:
            children = psutil.Process(daemon.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        assert children == [], "no upstream child may survive daemon shutdown"
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


# ── In-process regressions (no subprocess) ────────────────────────────────


async def _run_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    stop: asyncio.Event,
    *,
    wedged_stack: bool = False,
) -> tuple[asyncio.Task[None], Any]:
    """Drive run() in-process with a fake upstream; return (task, tracked stack)."""
    from mcp import types

    import services.browser.mcp_daemon as daemon_mod
    from services.browser.mcp_daemon import run

    sock = home / "chrome-mcp.1.sock"
    monkeypatch.setattr(daemon_mod, "chrome_mcp_socket", lambda: sock)

    async def socket_free(_path: Path) -> bool:
        return False

    monkeypatch.setattr(daemon_mod, "_socket_in_use", socket_free)

    class FakeSession:
        async def send_ping(self) -> None:
            await asyncio.sleep(0)

        async def list_tools(self) -> Any:
            return type("R", (), {"tools": []})()

        async def call_tool(self, *_a: object, **_k: object) -> Any:
            return types.CallToolResult(content=[], is_error=False)

    class TrackedStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            if wedged_stack:
                await asyncio.Event().wait()

    stack = TrackedStack()

    async def fake_create_upstream(_url: str, _stop: asyncio.Event) -> tuple[Any, Any]:
        return FakeSession(), stack  # type: ignore[return-value]

    monkeypatch.setattr(daemon_mod, "_create_upstream", fake_create_upstream)

    async def never() -> None:
        await asyncio.Event().wait()

    session_task = asyncio.create_task(never())

    async def fake_start_maintenance() -> tuple[asyncio.Event, asyncio.Task[None]]:
        return stop, session_task

    monkeypatch.setattr(daemon_mod, "_start_session_maintenance", fake_start_maintenance)
    monkeypatch.setattr(daemon_mod, "_spawn_inject", lambda: None)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)  # let run() connect and start the watchdog/reaper
    return task, stack


@pytest.mark.flaky
def test_cancelled_session_task_never_skips_cleanup(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2043 regression: a cancelled session task must not escape the finally.

    The old ``suppress(Exception)`` let CancelledError propagate out of run()
    and skip the upstream stack / server / socket cleanup entirely.
    """
    import services.browser.mcp_daemon as daemon_mod

    async def scenario() -> None:
        stop = asyncio.Event()
        task, stack = await _run_with_fakes(monkeypatch, home, stop)
        await asyncio.sleep(0.2)
        stop.set()  # SIGTERM equivalent
        await asyncio.wait_for(task, timeout=20)
        assert stack.closed, "upstream stack must be closed after the task ended"
        assert not (home / "chrome-mcp.1.sock").exists(), "socket must be unlinked"

    asyncio.run(scenario())
    assert daemon_mod  # module imported for the monkeypatch lifetime


@pytest.mark.flaky
def test_wedged_cleanup_await_is_bounded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup step that never finishes cannot hold the shutdown open."""

    async def scenario() -> None:
        stop = asyncio.Event()
        task, _stack = await _run_with_fakes(monkeypatch, home, stop, wedged_stack=True)
        await asyncio.sleep(0.2)
        start = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=30)
        assert time.monotonic() - start < 20, "wedged stack close must be bounded"
        assert not (home / "chrome-mcp.1.sock").exists()

    asyncio.run(scenario())
