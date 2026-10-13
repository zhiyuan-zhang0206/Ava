"""`base/db` connection policy: placeholder refusal, keepalive kwargs, direct dials and pool arming."""

from __future__ import annotations

import threading
from functools import partial
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base import db
from base.config import settings
from base.db import connections
from base.db.code_version_gate import ProcessDbGate
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

_PROCESS = "db-test"


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
    database_gate = ProcessDbGate(version=lambda: 7, process=_PROCESS)
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
    assert check.func is connections.restore_pooled_session_async
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
            "application_name": f"ava:{_PROCESS}:v7",
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
    monkeypatch.setattr(connections, "restore_pooled_session", no_scrub)
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
    dials = _spy_dials(monkeypatch)
    db.connect(direct=True, unbounded=True, gate=database_gate, direct_url=_direct_url_x)
    assert dials == [
        (
            "postgresql://u:p@db:5432/x",
            {"autocommit": False, "prepare_threshold": None, **db.PG_KEEPALIVE_KWARGS},
        )
    ]


def test_connect_url_refuses_placeholder_url() -> None:
    with pytest.raises(db.PlaceholderDbUrlError):
        db.connect_url(PLACEHOLDER_DB_URL)


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
    # Pooled (the default): the baseline restore is armed on configure + check.
    db.pool(gate=database_gate)
    configure = captured["configure"]
    check = captured["check"]
    assert isinstance(configure, partial) and isinstance(check, partial)
    assert configure.func is connections.restore_pooled_session
    assert check.func is connections.restore_pooled_session
    assert configure.keywords == check.keywords == {"gate": database_gate}
    captured.clear()
    # Direct: no scrub; the flag keeps arming the plain dead-connection check.
    db.pool(direct=True, check_connections=True, gate=database_gate, direct_url=_direct_url_test)
    assert captured.get("configure") is None
    assert captured.get("check") is real_check
    captured.clear()
    db.pool(direct=True, gate=database_gate, direct_url=_direct_url_test)
    assert captured.get("check") is None
