"""The dead show() page scan: a loop of the page-server service that closes the rows
of agent-owned page servers that died and tells the owner once."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.daemon.endpoints import ServiceEndpoint
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from services.page_server import daemon, dead_pages
from services.page_server.tests.slices import page_server_config
from tests.fixtures.units import spawn_agent

_HOST = "127.0.0.1"


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Record the PageClosed frames and the wakes the pass publishes."""
    seen: dict[str, list[Any]] = {"events": [], "wakes": []}

    async def publish(_bus: object, payload: str, *, context: str) -> None:
        seen["events"].append(payload)

    def wake(agent_id: int, inbound_id: str) -> None:
        seen["wakes"].append((agent_id, inbound_id))

    monkeypatch.setattr(EventBus, "publish_best_effort", publish)
    monkeypatch.setattr(dead_pages, "publish_inbound_wake", wake)
    monkeypatch.setattr(admission, "quiesced", lambda: False)
    return seen


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((_HOST, 0))
        return int(s.getsockname()[1])


def _page(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    *,
    serve_dir: str | None = None,
    host: str = _HOST,
) -> None:
    conn.execute(
        "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
        "VALUES (%s, %s, %s, %s, %s)",
        (agent_id, name, port, host, serve_dir),
    )
    conn.commit()


def _closed(conn: psycopg.Connection, agent_id: int, name: str) -> bool:
    row = conn.execute(
        "SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s AND name = %s",
        (agent_id, name),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def _notices(conn: psycopg.Connection, agent_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
        (agent_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _progress() -> LoopProgress:
    return LoopProgress("dead_show_pages", 600.0)


async def _round(pool: ConnectionPool) -> None:
    await dead_pages.dead_pages_round(pool, _HOST, _progress(), EventBus.from_settings())


def test_only_open_show_pages_of_this_host_are_selected(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = spawn_agent(spawner="user")
    _page(db_conn, agent, "show-open", 18101)
    _page(db_conn, agent, "serve-open", 18102, serve_dir="/data/site")
    _page(db_conn, agent, "elsewhere", 18103, host="10.9.9.9")
    _page(db_conn, agent, "show-closed", 18104)
    db_conn.execute(
        "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND name = 'show-closed'",
        (agent,),
    )
    db_conn.commit()

    assert [p.name for p in dead_pages.open_show_pages(pool, _HOST)] == ["show-open"]


async def test_a_dead_show_page_is_closed_and_its_owner_told_once(
    db_conn: psycopg.Connection, pool: ConnectionPool, published: dict[str, list[Any]]
) -> None:
    agent = spawn_agent(spawner="user")
    _page(db_conn, agent, "dead-one", _free_port())
    _page(db_conn, agent, "dead-two", _free_port())

    await _round(pool)

    assert _closed(db_conn, agent, "dead-one") and _closed(db_conn, agent, "dead-two")
    notices = _notices(db_conn, agent)
    assert len(notices) == 1  # one notice naming both pages
    assert "'dead-one'" in notices[0] and "'dead-two'" in notices[0]
    assert len(published["events"]) == 2  # a PageClosed per page
    assert published["wakes"] == [(agent, "0")]


async def test_the_owner_is_not_told_again_within_the_dedupe_window(
    db_conn: psycopg.Connection, pool: ConnectionPool, published: dict[str, list[Any]]
) -> None:
    agent = spawn_agent(spawner="user")
    _page(db_conn, agent, "first", _free_port())
    await _round(pool)
    _page(db_conn, agent, "second", _free_port())

    await _round(pool)

    assert _closed(db_conn, agent, "second")  # still closed ...
    assert len(_notices(db_conn, agent)) == 1  # ... but the agent is not nagged again
    assert published["wakes"] == [(agent, "0")]


async def test_a_live_show_page_and_a_dead_serve_page_are_left_alone(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    published: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live show() server is kept; a serve() page is the daemon's own to relaunch,
    so a dead one is neither probed nor closed here."""
    agent = spawn_agent(spawner="user")
    alive_port = _free_port()
    _page(db_conn, agent, "alive", alive_port)
    _page(db_conn, agent, "serve-dead", _free_port(), serve_dir="/data/site")
    probed: list[int] = []

    def alive(_host: str, port: int) -> bool:
        probed.append(port)
        return port == alive_port

    monkeypatch.setattr(dead_pages, "page_server_alive", alive)

    await _round(pool)

    assert probed == [alive_port]
    assert not _closed(db_conn, agent, "alive") and not _closed(db_conn, agent, "serve-dead")
    assert _notices(db_conn, agent) == [] and published["events"] == []


async def test_a_quiesced_unit_skips_the_round(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    published: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = spawn_agent(spawner="user")
    _page(db_conn, agent, "dead", _free_port())
    monkeypatch.setattr(admission, "quiesced", lambda: True)

    await _round(pool)

    assert not _closed(db_conn, agent, "dead")


async def test_a_failed_close_rolls_back_the_close_and_the_notice(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    published: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close and notice are one transaction: when the notice cannot be written the row
    stays open for the next round, so the agent is never told about an open row."""
    agent = spawn_agent(spawner="user")
    _page(db_conn, agent, "dead", _free_port())
    monkeypatch.setattr(
        dead_pages.page_recovery, "NOTICE_INSERT_SQL", "INSERT INTO nowhere VALUES (%s, %s)"
    )

    with pytest.raises(psycopg.ProgrammingError):
        await _round(pool)

    assert not _closed(db_conn, agent, "dead")
    assert published["events"] == []


async def test_the_loop_scans_at_once_then_paces(monkeypatch: pytest.MonkeyPatch) -> None:
    rounds: list[object] = []

    async def fake_round(_pool: object, host: str, _progress: object, _bus: object) -> None:
        rounds.append(host)

    monkeypatch.setattr(dead_pages, "dead_pages_round", fake_round)
    task = asyncio.create_task(
        dead_pages.dead_pages_loop(
            cast(ConnectionPool, object()),
            _HOST,
            _progress(),
            page_server_config(),
            EventBus.from_settings(),
        )
    )
    try:
        for _ in range(200):
            if rounds:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert rounds == [_HOST]  # a scan at once; the heartbeat-interval wait never elapses


# --- the service owns both loops ---------------------------------------------


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, loops: dict[str, Callable[..., Any]], events: list[str]
) -> dict[str, object]:
    seen: dict[str, object] = {}

    class _Pool:
        def close(self) -> None:
            events.append("pool")

    async def fake_start(_name: str, _port: int, *, liveness: LivenessGroup) -> object:
        seen["trackers"] = sorted(liveness.snapshot())
        return object()

    async def fake_stop(_server: object) -> None:
        events.append("health")

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(
        daemon, "_endpoint", lambda: ServiceEndpoint("page_server", 1, Path("/nonexistent/ps.pid"))
    )
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: events.append("pidfile"))
    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)

    def fake_pool(_self: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr(daemon.Database, "pool", fake_pool)
    monkeypatch.setattr(daemon, "_reconcile_loop", loops["reconcile"])
    monkeypatch.setattr(daemon.dead_pages, "dead_pages_loop", loops["dead"])
    return seen


def test_each_loop_gets_its_own_progress_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, LoopProgress] = {}

    async def reconcile(
        _pool: object, progress: LoopProgress, _config: object, _bus: object
    ) -> None:
        received["reconcile"] = progress

    async def dead(
        _pool: object, _host: str, progress: LoopProgress, _config: object, _bus: object
    ) -> None:
        received["dead"] = progress

    seen = _patch_run(monkeypatch, {"reconcile": reconcile, "dead": dead}, [])

    asyncio.run(asyncio.wait_for(daemon.run(), timeout=5.0))

    assert seen["trackers"] == ["dead_show_pages", "reconcile"]
    assert len({id(p) for p in received.values()}) == 2


@pytest.mark.parametrize("crashing", ["reconcile", "dead"])
def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    monkeypatch: pytest.MonkeyPatch, crashing: str
) -> None:
    cancelled: list[str] = []
    events: list[str] = []

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run_loop

    _patch_run(monkeypatch, {"reconcile": loop("reconcile"), "dead": loop("dead")}, events)

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(asyncio.wait_for(daemon.run(), timeout=5.0))

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} crashed"]
    assert cancelled == [({"reconcile", "dead"} - {crashing}).pop()]
    assert sorted(events) == ["health", "pidfile", "pool"]
