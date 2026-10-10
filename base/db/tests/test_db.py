"""`base/db/__init__.py` live-agent helpers — the SQL the fleet update's quiesce step drives.

These are the relocated home of the agents_meta / inbound_messages queries the
gateway CLI used to hand-write inline: signal_live_agents_restart (bulk
restart), list_live_agent_ids. "Live" = status running/idling.
Each helper opens its own connection, so it sees rows committed by the fixture.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base import db
from base.config import settings
from base.db import Database, connections
from base.db.code_version_gate import ProcessDbGate
from base.db.tests.live_agents import seed_agent
from base.events.live.bus import EventBus
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.telemetry import Event, process_name


def _direct_url_x(_config: object = None, **_kwargs: object) -> str:
    return "postgresql://u:p@db:5432/x"


def _direct_url_test(_config: object = None, **_kwargs: object) -> str:
    return "postgresql://direct-test"


def test_connect_refuses_placeholder_url(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() raises PlaceholderDbUrlError when db_url is the placeholder URL,
    rather than letting a bare process reach a real database."""
    monkeypatch.setattr(settings.data_plane, "db_url", PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.connect(gate=database_gate)


def test_pool_refuses_placeholder_url(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.pool(gate=database_gate)


def test_async_pool_refuses_placeholder_url(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.async_pool(AsyncConnectionPool, min_size=0, max_size=1, timeout=1.0, gate=database_gate)


def test_async_pool_fixes_the_transport_posture(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async pool carries `pool()`'s posture: autocommit, no prepared
    statements, keepalives, the configured sslmode when the URL is silent, and
    the pooled-session scrub on every borrow, and the process/version name
    PgBouncer shows. It comes back unopened (the caller's event loop opens it),
    and the caller's subclass gets its own arguments."""
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://u@127.0.0.1:1/x")
    monkeypatch.setattr(settings.data_plane, "db_sslmode", "require")
    database_gate = ProcessDbGate(version=lambda: 7, process=process_name())
    captured: dict[str, object] = {}

    class _FakePool:
        def __init__(self, conninfo: str, **kw: object) -> None:
            captured.update(kw, conninfo=conninfo)

    db.async_pool(
        cast(Any, _FakePool),
        pool_name="probe",
        min_size=0,
        max_size=3,
        timeout=2.0,
        gate=database_gate,
    )
    check = captured["check"]
    assert isinstance(check, partial)
    assert check.func is connections._restore_pooled_session_async
    assert check.keywords == {"gate": database_gate}
    assert captured == {
        "conninfo": "postgresql://u@127.0.0.1:1/x",
        "min_size": 0,
        "max_size": 3,
        "timeout": 2.0,
        "open": False,
        "kwargs": {
            "autocommit": True,
            "prepare_threshold": None,
            "sslmode": "require",
            "application_name": f"ava:{process_name()}:v7",
            **db.PG_KEEPALIVE_KWARGS,
        },
        "check": captured["check"],
        "pool_name": "probe",
    }


def _spy_dials(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record each psycopg.connect (conninfo, kwargs) and refuse the pooled scrub:
    the door must decide the posture without dialing anything real.

    Only the test's own thread is recorded: the patch sits on the process-wide
    ``psycopg.connect``, and a dial from another thread sharing the xdist worker
    must not enter the list (2026-10-03 shard8 flake)."""
    dials: list[tuple[str, dict[str, Any]]] = []
    owner = threading.current_thread()

    def spy(conninfo: str = "", **kwargs: Any) -> object:
        if threading.current_thread() is owner:
            dials.append((conninfo, kwargs))
        return object()

    def no_scrub(_conn: object) -> None:
        raise AssertionError("an explicit-target dial must not scrub a pooled session")

    monkeypatch.setattr(psycopg, "connect", spy)
    monkeypatch.setattr(connections, "_restore_pooled_session", no_scrub)
    return dials


def test_connect_url_owns_the_transport_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit-target door carries `connect()`'s posture — no prepared
    statements, keepalives, the statement ceiling after the URL's own startup
    options — and reads no settings: the configured sslmode is not injected and
    the session is never scrubbed."""
    monkeypatch.setattr(settings.data_plane, "db_sslmode", "require")
    dials = _spy_dials(monkeypatch)
    url = "postgresql://owner@/ava?host=/tmp/sock&options=-c%20role%3Dava"
    db.connect_url(url, autocommit=True)
    assert dials == [
        (
            url,
            {
                "autocommit": True,
                "prepare_threshold": None,
                **db.PG_KEEPALIVE_KWARGS,
                "options": "-c role=ava -c statement_timeout=60000",
            },
        )
    ]


def test_connect_url_unbounded_keeps_the_keepalives(monkeypatch: pytest.MonkeyPatch) -> None:
    """`unbounded=True` drops only the statement ceiling; a caller's shorter
    connect timeout replaces the door's default."""
    dials = _spy_dials(monkeypatch)
    db.connect_url("postgresql://u@127.0.0.1:1/x", unbounded=True, connect_timeout=2)
    assert dials == [
        (
            "postgresql://u@127.0.0.1:1/x",
            {
                "autocommit": False,
                "prepare_threshold": None,
                **db.PG_KEEPALIVE_KWARGS,
                "connect_timeout": 2,
            },
        )
    ]


def test_connect_unbounded_keeps_the_keepalives(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration applier's unbounded direct dial drops only the ceiling: a
    long DDL on a remote link is the flow a dead peer would otherwise pin."""
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://u:p@db.example:5432/x")
    monkeypatch.setattr(settings.data_plane, "db_sslmode", "")
    monkeypatch.setattr(connections, "direct_db_url", _direct_url_x)
    dials = _spy_dials(monkeypatch)
    db.connect(direct=True, unbounded=True, gate=database_gate)
    assert dials == [
        (
            "postgresql://u:p@db:5432/x",
            {"autocommit": False, "prepare_threshold": None, **db.PG_KEEPALIVE_KWARGS},
        )
    ]


def test_connect_url_refuses_placeholder_url() -> None:
    with pytest.raises(db.PlaceholderDbUrlError):
        db.connect_url(PLACEHOLDER_DB_URL)


def _inbound_rows(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, content FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchall()


def test_insert_restart_completed_inbound_traces_newest_restart(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion marker retains the restart envelope the claim will render."""
    agent_id = seed_agent(db_conn, "idling")
    payload = {"config_overlay": {"model": "gpt-5"}}
    post_commit_events: list[Event] = []
    emitted: list[Event] = []
    monkeypatch.setattr(db, "_emit_prepared_event", emitted.append)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'restart', 'self', %s::jsonb)",
            (agent_id, "restart with a new model", json.dumps(payload)),
        )
        traced = db.insert_restart_completed_inbound(
            cur, agent_id, post_commit_events=post_commit_events
        )
        assert emitted == []
    db_conn.commit()
    for event in post_commit_events:
        db._emit_prepared_event(event)

    assert traced == ("self", "restart with a new model", payload)
    assert len(emitted) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, content, payload FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        assert cur.fetchall() == [
            ("restart", "self", "restart with a new model", payload),
            ("restart_completed", "self", "restart with a new model", payload),
        ]


def test_insert_restart_completed_inbound_without_restart_returns_none(
    db_conn: psycopg.Connection,
) -> None:
    """Callers decide how to handle a missing restart inbound; the helper does not insert."""
    agent_id = seed_agent(db_conn, "idling")
    with db_conn.cursor() as cur:
        assert db.insert_restart_completed_inbound(cur, agent_id, post_commit_events=[]) is None
    db_conn.commit()

    assert _inbound_rows(db_conn, agent_id) == []


def test_signal_live_agents_restart_only_live(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """One restart inbound (content='', the given source) per running/idling agent;
    terminated get none. Returns the ids signalled."""
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    terminated = seed_agent(db_conn, "terminated")

    ids = db.signal_live_agents_restart(database, event_bus, source="system:update")

    assert sorted(ids) == sorted([running, idling])
    assert _inbound_rows(db_conn, running) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, idling) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_requires_an_unexpired_lease(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """R1 (Task #1021): a running/idling row WITHOUT a lease (pre-lease code) or
    with an EXPIRED one (a process that stopped renewing) is not alive — the
    lease is the liveness authority, and the quiesce must not signal a zombie."""
    running_no_lease = seed_agent(db_conn, "running", live_lease=False)
    idling_no_lease = seed_agent(db_conn, "idling", live_lease=False)
    running_fresh = seed_agent(db_conn, "running")

    ids = db.signal_live_agents_restart(database, event_bus, source="system:update")

    assert ids == [running_fresh]
    assert _inbound_rows(db_conn, running_no_lease) == []
    assert _inbound_rows(db_conn, idling_no_lease) == []


def test_agent_is_alive_predicate() -> None:
    """The Python half of the single alive predicate — status AND unexpired
    lease, one definition for row-based checks."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    future = now + timedelta(seconds=600)
    past = now - timedelta(seconds=1)

    assert db.agent_is_alive("running", future) is True
    assert db.agent_is_alive("idling", future) is True
    assert db.agent_is_alive("running", None) is False  # pre-lease row
    assert db.agent_is_alive("running", past) is False  # expired
    assert db.agent_is_alive("terminated", future) is False


def test_signal_live_agents_restart_none_live(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """No live agents → no inbound inserted, returns []."""
    terminated = seed_agent(db_conn, "terminated")
    assert db.signal_live_agents_restart(database, event_bus, source="system:update") == []
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_exclude_ids(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """exclude_agent_ids agents are skipped even when live — the quiesce
    convergence loop passes its already-signalled set so a pass only signals
    newly-live agents."""
    already = seed_agent(db_conn, "running")
    late = seed_agent(db_conn, "running")

    ids = db.signal_live_agents_restart(
        database, event_bus, source="system:update", exclude_agent_ids={already}
    )

    assert ids == [late]
    assert _inbound_rows(db_conn, already) == []
    assert _inbound_rows(db_conn, late) == [("restart", "system:update", "")]


def test_list_live_agent_ids(db_conn: psycopg.Connection, database: Database) -> None:
    """list_live_agent_ids lists agents with a LIVE process to act on (quiesce):
    running/idling only."""
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    seed_agent(db_conn, "terminated")
    assert sorted(db.list_live_agent_ids(database)) == sorted([running, idling])


def test_pool_check_connections_flag(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POOLED pool arms the baseline-session restore on every checkout (and at
    backend creation) — pgbouncer never resets backend session state between
    clients, so a backend polluted by another client's session-level SET must be
    scrubbed before each borrow (2026-09-02 P0). The restore doubles as the
    Task #1027 dead-connection check (a dead connection raises and is replaced).
    A DIRECT pool owns its backend exclusively — no scrub needed — and there the
    `check_connections=True` flag keeps its original Task #1027 meaning."""
    real_check = connections.ConnectionPool.check_connection
    captured: dict[str, object] = {}

    class _FakePool:
        check_connection = real_check

        def __init__(self, *_a: object, **_kw: object) -> None:
            captured.update(_kw)

    monkeypatch.setattr(connections, "ConnectionPool", _FakePool)
    monkeypatch.setattr(connections, "direct_db_url", _direct_url_test)
    # Pooled (the default): the baseline restore is armed on configure + check.
    db.pool(gate=database_gate)
    configure = captured["configure"]
    check = captured["check"]
    assert isinstance(configure, partial) and isinstance(check, partial)
    assert configure.func is connections._restore_pooled_session
    assert check.func is connections._restore_pooled_session
    assert configure.keywords == check.keywords == {"gate": database_gate}
    captured.clear()
    # Direct: no scrub; the flag keeps arming the plain dead-connection check.
    db.pool(direct=True, check_connections=True, gate=database_gate)
    assert captured.get("configure") is None
    assert captured.get("check") is real_check
    captured.clear()
    db.pool(direct=True, gate=database_gate)
    assert captured.get("check") is None
