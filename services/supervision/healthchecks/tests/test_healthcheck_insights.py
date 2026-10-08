"""`services.supervision.healthchecks.insights`: identity over the Unix socket."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from base.paths import ava_home
from services.derived.insights.daemon import bind_socket
from services.supervision.healthchecks import insights


def _app(name: str = "insights", pid: int | None = None, home: str | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "name": name,
            "pid": os.getpid() if pid is None else pid,
            "home": str(ava_home()) if home is None else home,
        }

    return app


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # AF_UNIX paths are about 100 bytes at most; pytest's tmp_path can exceed that.
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        root = Path(name)
        monkeypatch.setattr(insights, "insights_socket", lambda: root / "i.sock")
        monkeypatch.setattr(insights, "insights_pidfile", lambda: root / "i.pid")
        yield root


def _serve(root: Path, app: FastAPI) -> tuple[uvicorn.Server, threading.Thread]:
    sock = bind_socket(root / "i.sock")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", log_config=None))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    # The probe must not race the server's startup: the socket answers only once it is serving.
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, thread


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


def test_alive_when_the_socket_names_this_service_home_and_recorded_pid(served: Path) -> None:
    (served / "i.pid").write_text(f"{os.getpid()}\n")
    server, thread = _serve(served, _app())
    try:
        assert insights._probe().alive
    finally:
        _stop(server, thread)


@pytest.mark.parametrize(
    "app",
    [_app(name="other"), _app(home="/elsewhere"), _app(pid=1)],
    ids=["other service", "other home", "unrecorded pid"],
)
def test_not_alive_for_an_answer_that_is_not_ours(served: Path, app: FastAPI) -> None:
    (served / "i.pid").write_text(f"{os.getpid()}\n")
    server, thread = _serve(served, app)
    try:
        assert not insights._probe().alive
    finally:
        _stop(server, thread)


def test_not_alive_without_a_listener(served: Path) -> None:
    assert not insights._probe().alive
