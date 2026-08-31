"""Gateway startup scan — show() page owners notified after a host restart.

Covers `gateway/pages_recovery.py`: only open show() rows (serve_dir NULL)
on THIS host are candidates; only rows whose server does not answer a TCP
probe are notified; each owner gets ONE message listing all its dead pages;
terminated owners and owners already told within the min interval are
skipped; the scan never raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from gateway import pages_recovery
from shared.db import create_agent


@pytest.fixture()
def recovery_pool() -> Iterator[ConnectionPool]:
    """The scan's own small pool (functions take a ConnectionPool)."""
    import shared.db

    pool = shared.db.pool(max_size=2)
    yield pool
    pool.close()


@pytest.fixture
def live_page_server(tmp_path: Path) -> Iterator[int]:
    """A real TCP server on 127.0.0.1 — the "still alive" probe target."""
    import http.server
    import socketserver
    import threading

    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731 — factory for ThreadingTCPServer  # pyright: ignore[reportUnknownLambdaType, reportUnknownVariableType]
        *args,  # pyright: ignore[reportUnknownArgumentType]
        directory=str(tmp_path),
        **kwargs,  # pyright: ignore[reportUnknownArgumentType]
    )
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)  # pyright: ignore[reportUnknownArgumentType]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    """A port with nothing listening: bind then release."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _open_show_row(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    *,
    port: int,
    host: str = "127.0.0.1",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, %s, %s, %s, NULL)",
            (agent_id, name, port, host),
        )
    conn.commit()


def _open_serve_row(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    *,
    port: int,
    host: str = "127.0.0.1",
) -> None:
    """A daemon-supervised serve() row — must NOT be scanned (serve_dir set)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, %s, %s, %s, '/tmp/serve-dir')",
            (agent_id, name, port, host),
        )
    conn.commit()


def _running_agent(conn: psycopg.Connection) -> int:
    aid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    conn.commit()
    return aid


def _system_inbounds(conn: psycopg.Connection, agent_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]


def test_scan_notifies_owner_of_dead_show_page(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """An open show() row whose server is down gets its owner ONE notice."""
    aid = _running_agent(db_conn)
    port = _free_port()
    _open_show_row(db_conn, aid, "docs", port=port)

    notified = pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1")

    assert notified == [aid]
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1
    assert msgs[0].startswith("Page recovery:")
    assert "docs" in msgs[0]
    assert "ava.ui.show()" in msgs[0]


def test_scan_merges_pages_of_same_agent_into_one_message(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """Two dead pages of one agent -> ONE notice naming both."""
    aid = _running_agent(db_conn)
    _open_show_row(db_conn, aid, "a", port=_free_port())
    _open_show_row(db_conn, aid, "b", port=_free_port())

    notified = pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1")

    assert notified == [aid]
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1
    assert "a" in msgs[0] and "b" in msgs[0]


def test_scan_skips_live_server(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool, live_page_server: int
) -> None:
    """A show row whose server still answers is NOT notified — a gateway-only
    restart (agents alive) must not nag healthy owners."""
    aid = _running_agent(db_conn)
    _open_show_row(db_conn, aid, "live", port=live_page_server)

    notified = pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1")

    assert notified == []
    assert _system_inbounds(db_conn, aid) == []


def test_scan_skips_terminated_owner(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """A terminated agent is never resurrected by a recovery notice."""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'terminated')",
            (aid,),
        )
    db_conn.commit()
    _open_show_row(db_conn, aid, "dead", port=_free_port())

    notified = pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1")

    assert notified == []
    assert _system_inbounds(db_conn, aid) == []


def test_scan_skips_recently_notified_owner(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """An owner already told within the min interval is skipped (startup
    storm guard: a rollout restarting the gateway several times must not
    re-nag); after the interval the notice fires again."""
    aid = _running_agent(db_conn)
    port = _free_port()
    _open_show_row(db_conn, aid, "docs", port=port)

    assert pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1") == [aid]
    # Second pass within the interval: nothing new.
    assert pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1") == []
    assert len(_system_inbounds(db_conn, aid)) == 1

    # Age the notice past the interval -> the next pass notifies again.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = now() - %s "
            "WHERE agent_id = %s AND content LIKE 'Page recovery:%%'",
            (timedelta(seconds=pages_recovery._NOTICE_MIN_INTERVAL_S + 60), aid),
        )
    db_conn.commit()
    assert pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1") == [aid]
    assert len(_system_inbounds(db_conn, aid)) == 2


def test_scan_skips_serve_rows_and_other_hosts(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """Daemon-supervised serve() rows and rows on other hosts are out of scope."""
    aid = _running_agent(db_conn)
    _open_serve_row(db_conn, aid, "served", port=_free_port())
    _open_show_row(db_conn, aid, "remote", port=_free_port(), host="10.99.0.9")

    notified = pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1")

    assert notified == []
    assert _system_inbounds(db_conn, aid) == []


def test_scan_skips_closed_and_expired_rows(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool
) -> None:
    """Rows the agent closed or the TTL reaper expired are not candidates."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, closed_at) "
            "VALUES (%s, 'closed', %s, '127.0.0.1', now())",
            (aid, _free_port()),
        )
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, expired_at) "
            "VALUES (%s, 'expired', %s, '127.0.0.1', now())",
            (aid, _free_port()),
        )
    db_conn.commit()

    assert pages_recovery.notify_stale_show_owners_blocking(recovery_pool, "127.0.0.1") == []


async def test_run_show_page_recovery_never_raises(
    db_conn: psycopg.Connection, recovery_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async startup entry fails open: a scan failure logs, never raises."""

    def _boom(pool: ConnectionPool, host: str) -> list[int]:
        raise RuntimeError("db gone")

    monkeypatch.setattr(pages_recovery, "notify_stale_show_owners_blocking", _boom)
    await pages_recovery.run_show_page_recovery(recovery_pool, "127.0.0.1")
