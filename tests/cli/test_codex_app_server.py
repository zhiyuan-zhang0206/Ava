"""Live delivery into a running codex app server: the relay's fast path.

The fake app server speaks codex's own transport (JSON-RPC over a websocket on
a unix socket). The contract pinned here: initialize then initialized, one
``turn/start``; a result means delivered, while a refusal, a timeout, a dead
endpoint or an unsupported endpoint shape returns a reason the relay reports
as failure. The relay-level Steer-only wiring lives in
``test_impersonation_bridge.py``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from websockets.sync.server import Server, ServerConnection, unix_serve

from cli.commands import codex_app_server

THREAD_ID = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")


def _endpoint(path: Path) -> str:
    return f"unix://{path}"


@pytest.fixture
def short_socket() -> Iterator[Path]:
    """A bind path short enough for macOS's 104-char AF_UNIX limit.

    pytest's ``tmp_path`` is too deep on macOS (test__mcps_daemon precedent:
    the limit bites during bind, not connect); a short /tmp directory keeps
    every bind path legal on both platforms. Self-managed and removed on
    teardown.
    """
    # Short bind path: macOS AF_UNIX limit is 104 chars, CI Linux 107.
    base = Path(tempfile.mkdtemp(prefix="cxas-", dir="/tmp"))
    try:
        yield base / "codex.sock"
    finally:
        shutil.rmtree(base, ignore_errors=True)


class FakeAppServer:
    """Minimal codex app server: JSON-RPC over a unix-socket websocket.

    ``interleave`` messages are sent ahead of the turn/start reply — the
    notifications and server-initiated requests the client must skip;
    ``hold_reply`` drops the reply so only the interleave ever arrives.
    """

    def __init__(
        self,
        *,
        silent: bool = False,
        refuse_turn: bool = False,
        interleave: Sequence[dict[str, Any]] = (),
        hold_reply: bool = False,
    ) -> None:
        self.silent = silent
        self.refuse_turn = refuse_turn
        self.interleave = list(interleave)
        self.hold_reply = hold_reply
        self.received: list[dict[str, Any]] = []
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, path: Path) -> None:
        self._server = unix_serve(
            self._handle, path=str(path), open_timeout=5, close_timeout=1, compression=None
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _handle(self, conn: ServerConnection) -> None:
        if self.silent:
            for _ in conn:  # accepted, never answered: drain until the client gives up
                pass
            return
        for raw in conn:
            message = json.loads(raw)
            self.received.append(message)
            method = message.get("method")
            if method == "initialize":
                conn.send(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}))
            elif method == "turn/start":
                for extra in self.interleave:
                    conn.send(json.dumps(extra))
                if self.hold_reply:
                    continue
                if self.refuse_turn:
                    conn.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32603,
                                    "message": (
                                        "failed to submit turn input: "
                                        "ActiveTurnNotSteerable { turn_kind: Review }"
                                    ),
                                },
                            }
                        )
                    )
                else:
                    conn.send(
                        json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    )


def test_live_submit_delivers_over_the_control_socket(short_socket: Path) -> None:
    server = FakeAppServer()
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "hello host", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is None
    assert [m.get("method") for m in server.received] == [
        "initialize",
        "initialized",
        "turn/start",
    ]
    assert server.received[0]["params"]["clientInfo"]["name"] == "ava-impersonation-relay"
    assert server.received[-1]["params"] == {
        "threadId": str(THREAD_ID),
        "input": [{"type": "text", "text": "hello host"}],
    }


def test_live_submit_reports_a_refused_turn(short_socket: Path) -> None:
    server = FakeAppServer(refuse_turn=True)
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "parked", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is not None
    assert "turn/start refused" in reason
    assert "ActiveTurnNotSteerable" in reason


def test_live_submit_skips_interleaved_server_traffic(short_socket: Path) -> None:
    spoof = {"id": codex_app_server._TURN_REQUEST_ID, "method": "item/requestApproval"}
    server = FakeAppServer(
        interleave=[
            {"method": "remoteControl/status/changed", "params": {"status": "disabled"}},
            spoof,
        ]
    )
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "delivered anyway", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is None


def test_live_submit_does_not_mistake_a_server_request_for_its_reply(
    short_socket: Path,
) -> None:
    """An id collision without result/error is not a reply: the client times out."""
    spoof = {"id": codex_app_server._TURN_REQUEST_ID, "method": "item/requestApproval"}
    server = FakeAppServer(interleave=[spoof], hold_reply=True)
    server.start(short_socket)
    started = time.monotonic()
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "unanswered", endpoint=_endpoint(short_socket), timeout=0.3
        )
    finally:
        server.stop()
    assert reason is not None
    assert time.monotonic() - started < 5.0


def test_live_submit_bounds_a_silent_server(short_socket: Path) -> None:
    server = FakeAppServer(silent=True)
    server.start(short_socket)
    started = time.monotonic()
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "unheard", endpoint=_endpoint(short_socket), timeout=0.3
        )
    finally:
        server.stop()
    assert reason is not None
    assert time.monotonic() - started < 5.0


def test_live_submit_reports_an_unreachable_endpoint(tmp_path: Path) -> None:
    reason = codex_app_server.live_submit(
        str(THREAD_ID), "lost", endpoint=_endpoint(tmp_path / "missing.sock"), timeout=1.0
    )
    assert reason is not None


def test_live_submit_reports_an_unsupported_endpoint() -> None:
    reason = codex_app_server.live_submit(
        str(THREAD_ID), "nope", endpoint="tcp://host:1", timeout=1.0
    )
    assert reason is not None
    assert "unsupported" in reason


def test_default_control_endpoint_follows_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_app_server.default_control_endpoint() is None
    sock = tmp_path / "app-server-control" / "app-server-control.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    assert codex_app_server.default_control_endpoint() == f"unix://{sock}"


def test_live_submit_treats_a_bare_unix_endpoint_as_the_default_socket(
    short_socket: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``unix://`` resolves through ``default_control_socket()``.

    Its CODEX_HOME mapping stays covered by
    ``test_default_control_endpoint_follows_codex_home``; the bind goes through
    the short-socket fixture like every other test that starts a fake server.
    """

    def short_default_socket() -> Path:
        return short_socket

    monkeypatch.setattr(codex_app_server, "default_control_socket", short_default_socket)
    server = FakeAppServer()
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(str(THREAD_ID), "hi", endpoint="unix://", timeout=3.0)
    finally:
        server.stop()
    assert reason is None
