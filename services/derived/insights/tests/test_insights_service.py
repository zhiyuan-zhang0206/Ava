"""The insights app and its socket: the routes it serves, and how the daemon binds them."""

from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, HumanMessage
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock, ClockConfig
from base.config import ConfigBoot
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.derived.insights import daemon
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.derived.insights.daemon import bind_socket
from services.derived.insights.run_timeline import history

_CONFIG = InsightsConfig(run_timeline_message_text_max=20)
_TIMELINE_INPUTS = TimelineReadInputs(
    lambda: Clock(ClockConfig("UTC", "UTC", False)), lambda: False
)


def _app(
    *, model_catalog: ModelCatalog, timeline_inputs: TimelineReadInputs = _TIMELINE_INPUTS
) -> Any:
    return build_app(
        cast(Database, object()),
        cast(ConnectionPool[Any], object()),
        _CONFIG,
        catalog=model_catalog,
        default_model_reader=lambda: "deepseek-v4-flash-vision-exp",
        timeline_inputs=timeline_inputs,
    )


def test_the_app_serves_the_run_timeline_routes_at_their_public_paths(
    *, model_catalog: ModelCatalog
) -> None:
    paths = set(_app(model_catalog=model_catalog).openapi()["paths"])
    assert {
        "/api/agents/{agent_id}/run-timeline",
        "/api/agents/{agent_id}/run-timeline/messages",
        "/api/agents/{agent_id}/run-timeline/context",
    } <= paths


def test_healthz_names_the_service_home_and_process(*, model_catalog: ModelCatalog) -> None:
    body = TestClient(_app(model_catalog=model_catalog)).get("/healthz").json()
    assert body["name"] == "insights"
    assert isinstance(body["pid"], int)


def test_a_route_validates_before_it_reads_anything(*, model_catalog: ModelCatalog) -> None:
    client = TestClient(_app(model_catalog=model_catalog))
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


def test_the_app_answers_over_the_bound_socket(
    short_dir: Path, *, model_catalog: ModelCatalog
) -> None:
    path = short_dir / "insights.sock"
    sock: socket.socket = bind_socket(path)
    server = uvicorn.Server(
        uvicorn.Config(
            _app(model_catalog=model_catalog),
            log_level="warning",
            access_log=False,
            log_config=None,
        )
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


class RenderingPolicy:
    """A live reader with an observable clock, independent of process configuration."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.reads = 0
        self.clock_reads = 0
        self.error: ValueError | None = None
        self.inputs = TimelineReadInputs(self.clock, self.timestamps)

    def timestamps(self) -> bool:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.enabled

    def clock(self) -> Clock:
        self.clock_reads += 1
        return Clock(
            ClockConfig("UTC", "UTC", False),
            now=lambda: datetime(2026, 10, 10, 12, tzinfo=UTC),
        )


def _compact_history(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    loads: list[int] = []
    messages: list[BaseMessage] = [
        HumanMessage(
            content="summary",
            additional_kwargs={
                "ava_msg_type": "compact_summary",
                "ava_created_at": "2026-10-10T12:00:00+00:00",
            },
        )
    ]

    def load(_db: Database, agent_id: int) -> FullHistory:
        loads.append(agent_id)
        return single_segment_history(messages)

    monkeypatch.setattr(history, "load_checkpoint_history_full", load)

    def head(_db: Database, _agent: int) -> str:
        return "head"

    monkeypatch.setattr(history, "latest_checkpoint_id", head)
    return loads


def test_apps_keep_their_rendering_policy_and_cache_separate_and_live(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    loads = _compact_history(monkeypatch)
    hidden, shown = RenderingPolicy(enabled=False), RenderingPolicy(enabled=True)
    first = _app(model_catalog=model_catalog, timeline_inputs=hidden.inputs)
    second = _app(model_catalog=model_catalog, timeline_inputs=shown.inputs)
    assert first.state.timeline_inputs is hidden.inputs
    assert second.state.timeline_inputs is shown.inputs
    path = "/api/agents/9/run-timeline/messages?start=0&end=0&full=true"

    assert TestClient(first).get(path).json()["messages"][0]["parts"][0]["text"] == (
        "Compact summary:\n\nsummary"
    )
    assert TestClient(second).get(path).json()["messages"][0]["parts"][0]["text"] == (
        "Compact summary [2026-10-10 12:00:00]:\n\nsummary"
    )
    # A cold view derives read times and units; the route renders a third time.
    assert (hidden.reads, hidden.clock_reads) == (3, 0)
    assert (shown.reads, shown.clock_reads) == (3, 3)
    assert loads == [9, 9]

    shown.enabled = False
    assert TestClient(second).get(path).json()["messages"][0]["parts"][0]["text"] == (
        "Compact summary:\n\nsummary"
    )
    assert (shown.reads, shown.clock_reads) == (4, 3)
    assert loads == [9, 9]  # the raw rendering stays live while the view remains cached


def test_a_rendering_error_propagates_and_does_not_cache_a_failed_view(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    loads = _compact_history(monkeypatch)
    policy = RenderingPolicy(enabled=False)
    failure = ValueError("invalid rendering policy")
    policy.error = failure
    client = TestClient(_app(model_catalog=model_catalog, timeline_inputs=policy.inputs))
    path = "/api/agents/9/run-timeline/messages?start=0&end=0&full=true"
    with pytest.raises(ValueError) as caught:
        client.get(path)
    assert caught.value is failure

    policy.error = None
    assert client.get(path).status_code == 200
    assert loads == [9, 9]
    policy.error = failure
    with pytest.raises(ValueError) as caught:
        client.get(path)  # the cache hit still renders through the same live reader
    assert caught.value is failure
    assert loads == [9, 9]


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


@pytest.mark.asyncio
@pytest.mark.usefixtures("owned_config_environment")
async def test_daemon_binds_live_rendering_to_its_configuration_owner(
    monkeypatch: pytest.MonkeyPatch, short_dir: Path, *, model_catalog: ModelCatalog
) -> None:
    boot = ConfigBoot()
    boot.set_field("timezone", "Asia/Tokyo")
    boot.set_field("message_timestamp_weekday", False)
    boot.set_field("message_timestamps", False)
    boot.set_field("run_timeline_message_text_max", 37)
    apps: list[Any] = []
    closed: list[bool] = []
    pool = SimpleNamespace(close=lambda: closed.append(True))

    class Store:
        def pool(self, *, max_size: int) -> Any:
            assert max_size == 4
            return pool

    db = Store()

    class Server:
        def __init__(self, config: uvicorn.Config) -> None:
            apps.append(config.app)

        async def serve(self, *, sockets: list[socket.socket]) -> None:
            for sock in sockets:
                sock.close()

    monkeypatch.setattr(daemon, "insights_pidfile", lambda: short_dir / "insights.pid")
    monkeypatch.setattr(daemon, "insights_socket", lambda: short_dir / "insights.sock")
    monkeypatch.setattr(daemon.Database, "from_settings", lambda: db)
    monkeypatch.setattr(daemon, "build_model_catalog", lambda: model_catalog)
    monkeypatch.setattr(daemon.uvicorn, "Server", Server)
    await daemon.run(config=boot)

    assert len(apps) == 1
    assert apps[0].state.config.run_timeline_message_text_max == 37
    model_reader = apps[0].state.default_model_reader
    boot.set_field("llm_model", "first-process-model")
    assert model_reader() == "first-process-model"
    boot.set_field("llm_model", "updated-process-model")
    assert model_reader() == "updated-process-model"
    inputs: TimelineReadInputs = apps[0].state.timeline_inputs
    assert inputs.timestamps_enabled() is False
    assert inputs.clock_factory().format_timestamp(datetime(2026, 10, 10, 12, tzinfo=UTC)) == (
        "[2026-10-10 21:00:00]"
    )
    boot.set_field("message_timestamps", True)
    boot.set_field("timezone", "UTC")
    assert inputs.timestamps_enabled() is True
    assert inputs.clock_factory().format_timestamp(datetime(2026, 10, 10, 12, tzinfo=UTC)) == (
        "[2026-10-10 12:00:00]"
    )
    assert closed == [True]
    assert not (short_dir / "insights.pid").exists()
    assert not (short_dir / "insights.sock").exists()
