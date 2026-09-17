"""Real child-process regression for the gateway's bounded connection drain.

2026-09-17: the gateway stall was uvicorn's connection-drain phase
("Waiting for connections to close") entered without a
``timeout_graceful_shutdown`` — an unfinished streaming response held it until
a forced kill. That defect lives below the request handlers, so it needs a real
server, a real stream and a real signal: these tests start genuine child
processes from the production launch assembly
(``gateway._server.serve_kwargs``) and only swap the bind address, the port and
the ASGI app. Removing ``timeout_graceful_shutdown`` from the assembly makes the
stuck-stream test fail through the parent's own deadline (the child is killed —
the suite never hangs).

The test app mirrors the shape that stalled production: a streaming response
that only ends on client disconnect or task cancellation, with ``finally``
cleanup. The parent holds its client connection open through the whole
shutdown — closing it would make shutdown pass for the wrong reason.

The drain budget reaches the child through a throwaway `$AVA_HOME/.env`, the
operator's real channel; a bare environment variable is dropped for
cluster-scope fields before Settings reads it.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.responses import PlainTextResponse, StreamingResponse

from gateway import _server

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The production knob under test; the child resolves it through its settings.
_DRAIN_ENV = "AVA_GATEWAY_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"
_MARKERS_ENV = "GATEWAY_SHUTDOWN_TEST_MARKERS"
_PORT_ENV = "GATEWAY_SHUTDOWN_TEST_PORT"

# Bounds asserted by the parent. The drain budget is consumed only while a
# connection is unfinished, so measured exit after SIGTERM is
# drain + cancellation + lifespan + interpreter teardown. _EXIT_SLACK_S admits
# CI scheduling jitter while still separating a bounded drain from an unbounded
# one; _KILL_SLACK_S sits above it so a regression surfaces as a failed
# assertion carrying the child's log, not as a bare pytest hang.
_EXIT_SLACK_S = 12.0
_KILL_SLACK_S = 15.0


def _mark(name: str) -> None:
    """Append one marker line — the child-to-parent protocol."""
    with Path(os.environ[_MARKERS_ENV]).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    _mark("lifespan-startup")
    try:
        yield
    finally:
        _mark("lifespan-shutdown")


app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/stream")
async def _stream() -> StreamingResponse:
    """Endless SSE-shaped stream: an unfinished response, like the sse.py routes."""

    async def _frames() -> AsyncIterator[bytes]:
        _mark("stream-open")
        try:
            while True:
                yield b"data: tick\n\n"
                await asyncio.sleep(0.1)
        except (asyncio.CancelledError, GeneratorExit) as exc:
            # The cleanup path under test: uvicorn cancelled the request task
            # (CancelledError thrown while the task awaits inside the stream),
            # or the generator was closed afterwards (GeneratorExit). Record
            # HOW the unwind reached the generator, so the assertion proves the
            # cancellation path ran cleanup — not merely that a marker exists.
            _mark(f"stream-closed:{type(exc).__name__}")
            raise

    return StreamingResponse(_frames(), media_type="text/event-stream")


@app.get("/slow")
async def _slow(seconds: float = 1.0) -> PlainTextResponse:
    _mark("slow-started")
    await asyncio.sleep(seconds)
    _mark("slow-finished")
    return PlainTextResponse("slow-done")


def _run_child() -> None:
    """Child entry: a real uvicorn server from the production launch assembly."""
    kwargs = _server.serve_kwargs(host="127.0.0.1", app=f"{__name__}:app")
    kwargs["port"] = int(os.environ[_PORT_ENV])
    uvicorn.Server(uvicorn.Config(**kwargs)).run()


@dataclass
class _Child:
    proc: subprocess.Popen[bytes]
    port: int
    markers_path: Path
    log_path: Path
    log_file: IO[bytes]
    terminated_at: float | None = None

    def markers(self) -> str:
        if not self.markers_path.exists():
            return ""
        return self.markers_path.read_text(encoding="utf-8")

    def log_tail(self, limit: int = 4000) -> str:
        if not self.log_path.exists():
            return "(no child output)"
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]

    def wait_marker(self, name: str, *, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if name in self.markers():
                return
            if self.proc.poll() is not None:
                pytest.fail(
                    f"gateway child exited before marker {name!r} (rc={self.proc.returncode}):\n"
                    f"{self.log_tail()}"
                )
            time.sleep(0.05)
        self.kill()
        pytest.fail(f"gateway child never wrote marker {name!r}:\n{self.log_tail()}")

    def terminate(self) -> None:
        self.terminated_at = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)

    def wait_bounded_exit(self, *, drain_seconds: float, what: str) -> float:
        assert self.terminated_at is not None
        try:
            rc = self.proc.wait(timeout=drain_seconds + _KILL_SLACK_S)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(
                f"gateway child did not exit within {drain_seconds + _KILL_SLACK_S:.0f}s of "
                f"SIGTERM ({what}) — the connection drain is unbounded again. "
                f"Markers: {self.markers()!r}\nchild log tail:\n{self.log_tail()}"
            )
        elapsed = time.monotonic() - self.terminated_at
        # uvicorn restores the default handlers and re-raises the captured
        # SIGTERM after the graceful path completes, so a healthy run exits as
        # signal death (-SIGTERM), not 0.
        assert rc in (0, -signal.SIGTERM), (
            f"gateway child exited rc={rc} ({what}):\n{self.log_tail()}"
        )
        assert elapsed < drain_seconds + _EXIT_SLACK_S, (
            f"gateway child took {elapsed:.1f}s to exit after SIGTERM ({what}); "
            f"bound expected drain={drain_seconds:.1f}s + slack {_EXIT_SLACK_S:.0f}s"
        )
        return elapsed

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            with suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)

    def close(self) -> None:
        self.kill()
        self.log_file.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connect_with_retry(port: int, *, timeout_s: float = 15.0) -> http.client.HTTPConnection:
    deadline = time.monotonic() + timeout_s
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.connect()
            return conn
        except OSError as exc:
            conn.close()
            last_error = exc
            time.sleep(0.05)
    pytest.fail(f"gateway child never accepted a connection on 127.0.0.1:{port}: {last_error}")


def _spawn_child(tmp_path: Path, *, drain_seconds: float) -> _Child:
    port = _free_port()
    markers_path = tmp_path / "markers.txt"
    log_path = tmp_path / "child.log"
    # The child boots against a throwaway home whose .env declares the knobs —
    # the operator's real channel (a unit's .env is the single source of truth),
    # and the only one that reaches a cluster-scope field: the env-authority
    # pass drops undeclared cluster-scope keys from the child environment.
    home = tmp_path / "ava-home"
    home.mkdir()
    (home / ".env").write_text(
        f"{_DRAIN_ENV}={drain_seconds}\nAVA_GATEWAY_RELOAD=0\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AVA_HOME"] = str(home)
    env[_MARKERS_ENV] = str(markers_path)
    env[_PORT_ENV] = str(port)
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- fixed interpreter + in-repo entry, no shell
        [sys.executable, "-c", f"from {__name__} import _run_child; _run_child()"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    child = _Child(
        proc=proc,
        port=port,
        markers_path=markers_path,
        log_path=log_path,
        log_file=log_file,
    )
    child.wait_marker("lifespan-startup")
    return child


def test_sigterm_bounds_the_drain_with_a_stuck_stream(tmp_path: Path) -> None:
    """A live, never-finishing stream is cancelled at the budget; exit stays bounded."""
    drain = 2.0
    child = _spawn_child(tmp_path, drain_seconds=drain)
    conn: http.client.HTTPConnection | None = None
    try:
        conn = _connect_with_retry(child.port)
        conn.request("GET", "/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.readline().startswith(b"data:")
        assert "stream-open" in child.markers()
        child.terminate()
        # The client stays connected until after the child is gone: the drain
        # must be ended by uvicorn's budget, not by the client going away.
        child.wait_bounded_exit(drain_seconds=drain, what="stuck stream")
        markers = child.markers()
        closed = [line for line in markers.splitlines() if line.startswith("stream-closed:")]
        assert closed, "stream generator cleanup did not run after the cancellation"
        assert closed[0] in {"stream-closed:CancelledError", "stream-closed:GeneratorExit"}, (
            f"stream generator unwound by {closed[0]!r}, not the cancellation path"
        )
        assert "lifespan-shutdown" in markers, "lifespan shutdown did not run"
    finally:
        if conn is not None:
            conn.close()
        child.close()


def test_slow_request_finishing_inside_the_budget_completes(tmp_path: Path) -> None:
    """The budget lets an in-flight ordinary request finish before exit."""
    drain = 6.0
    child = _spawn_child(tmp_path, drain_seconds=drain)
    conn: http.client.HTTPConnection | None = None
    try:
        conn = _connect_with_retry(child.port)
        conn.request("GET", "/slow?seconds=3")
        child.wait_marker("slow-started")
        child.terminate()
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"slow-done"
        child.wait_bounded_exit(drain_seconds=drain, what="request finishing inside the budget")
        markers = child.markers()
        assert "slow-finished" in markers, "handler did not finish inside the budget"
        assert "lifespan-shutdown" in markers, "lifespan shutdown did not run"
    finally:
        if conn is not None:
            conn.close()
        child.close()


def test_slow_request_cut_by_the_budget_still_exits(tmp_path: Path) -> None:
    """A request that cannot finish inside the budget is cancelled; exit stays bounded."""
    drain = 2.0
    child = _spawn_child(tmp_path, drain_seconds=drain)
    conn: http.client.HTTPConnection | None = None
    try:
        conn = _connect_with_retry(child.port)
        conn.request("GET", "/slow?seconds=30")
        child.wait_marker("slow-started")
        child.terminate()
        child.wait_bounded_exit(drain_seconds=drain, what="request cut by the budget")
        markers = child.markers()
        assert "slow-finished" not in markers, "handler finished although its sleep exceeded budget"
        assert "lifespan-shutdown" in markers, "lifespan shutdown did not run"
    finally:
        if conn is not None:
            conn.close()
        child.close()
