"""`services.supervision.healthchecks.insights`: identity over the Unix socket."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from base.paths import insights_pidfile, insights_socket
from services.derived.insights.daemon import bind_socket
from services.supervision.healthchecks import insights


def _app(
    home: Path, *, name: str = "insights", pid: int | None = None, reported_home: str | None = None
) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "name": name,
            "pid": os.getpid() if pid is None else pid,
            "home": str(home) if reported_home is None else reported_home,
        }

    return app


@pytest.fixture
def served() -> Iterator[Path]:
    """A home under /tmp: AF_UNIX paths are about 100 bytes at most; pytest's tmp_path can exceed that."""
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        yield Path(name)


def _record_pid(home: Path) -> None:
    pidfile = insights_pidfile(home)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n")


def _serve(home: Path, app: FastAPI) -> tuple[uvicorn.Server, threading.Thread]:
    sock = bind_socket(insights_socket(home))  # the binder creates the run directory
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
    server, thread = _serve(served, _app(served))
    _record_pid(served)
    try:
        assert insights.insights_probe(served).alive
    finally:
        _stop(server, thread)


@pytest.mark.parametrize(
    "answer",
    [{"name": "other"}, {"reported_home": "/elsewhere"}, {"pid": 1}],
    ids=["other service", "other home", "unrecorded pid"],
)
def test_not_alive_for_an_answer_that_is_not_ours(served: Path, answer: dict[str, Any]) -> None:
    server, thread = _serve(served, _app(served, **answer))
    _record_pid(served)
    try:
        assert not insights.insights_probe(served).alive
    finally:
        _stop(server, thread)


def test_not_alive_without_a_listener(served: Path) -> None:
    assert not insights.insights_probe(served).alive
