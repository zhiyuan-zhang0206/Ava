"""The insights app and its socket: the routes it serves, and how the daemon binds them."""

from __future__ import annotations

import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.db import Database
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.derived.insights.daemon import bind_socket

_CONFIG = InsightsConfig(run_timeline_message_text_max=20)


def _app() -> Any:
    return build_app(cast(Database, object()), cast(ConnectionPool[Any], object()), _CONFIG)


def test_the_app_serves_the_run_timeline_routes_at_their_public_paths() -> None:
    paths = set(_app().openapi()["paths"])
    assert {
        "/api/agents/{agent_id}/run-timeline",
        "/api/agents/{agent_id}/run-timeline/messages",
        "/api/agents/{agent_id}/run-timeline/context",
    } <= paths


def test_healthz_names_the_service_home_and_process() -> None:
    body = TestClient(_app()).get("/healthz").json()
    assert body["name"] == "insights"
    assert isinstance(body["pid"], int)


def test_a_route_validates_before_it_reads_anything() -> None:
    client = TestClient(_app())
    # The state holds no database: only a request rejected at the boundary can answer.
    assert client.get("/api/agents/1/run-timeline/messages?start=3&end=1").status_code == 422
    assert client.get("/api/agents/1/run-timeline/messages?start=-1&end=1").status_code == 422
    assert client.get("/api/agents/1/nowhere").status_code == 404


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # AF_UNIX paths are about 100 bytes at most; pytest's tmp_path can exceed that.
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        yield Path(name)


def test_the_socket_is_owner_only_and_replaces_a_predecessors_leftover(short_dir: Path) -> None:
    path = short_dir / "insights.sock"
    path.write_text("left behind by a crashed predecessor")
    sock = bind_socket(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert mode == 0o600
    finally:
        sock.close()


def test_the_app_answers_over_the_bound_socket(short_dir: Path) -> None:
    path = short_dir / "insights.sock"
    sock: socket.socket = bind_socket(path)
    server = uvicorn.Server(
        uvicorn.Config(_app(), log_level="warning", access_log=False, log_config=None)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        transport = httpx.HTTPTransport(uds=str(path), retries=20)
        with httpx.Client(transport=transport, base_url="http://insights") as client:
            reply = client.get("/api/agents/1/run-timeline/messages?start=3&end=1")
        assert reply.status_code == 422
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
