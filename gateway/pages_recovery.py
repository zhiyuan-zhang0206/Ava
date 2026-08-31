"""One-shot gateway startup scan: notify owners of show() pages left dead by a host restart.

A show() page's server runs inside the agent's own process (started by the
agent, registered with serve_dir NULL); a host restart kills it with no
daemon to bring it back — the daemon only supervises serve() rows. The row
stays open and the reverse proxy 502s until the agent re-serves.

This module runs once at gateway startup, scanning THIS host's open show
rows and messaging each owner to re-serve. Guards:

- **Liveness probe** — only rows whose host:port actually refuses a TCP
  connect are notified. A gateway-only restart (agents alive, servers still
  serving) therefore notifies nobody; only genuinely dead servers do.
- **One message per agent** — an agent with several dead pages gets a single
  inbound listing them all.
- **Min interval** — an agent already told within ``_NOTICE_MIN_INTERVAL_S``
  (a repeated gateway restart while the server is still down) is skipped;
  the check reads the agent's own inbound history, so it survives restarts.
- **No resurrection** — only running/idling owners are notified, mirroring
  the TTL reaper.

Notification content is a system-sourced chat inbound (``source="system"``)
so the claim node delivers it as a plain message the agent can act on.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from shared.db import insert_inbound_message
from shared.log import logger
from shared.machine import reachable_host

_log = logging.getLogger(__name__)

# A local dead server refuses instantly; the bound covers a slow listener
# without delaying the startup scan meaningfully.
_PROBE_TIMEOUT_S = 1.0
# Repeated gateway restarts while a page stays dead must not re-nag the
# owner; the interval is checked against the agent's inbound history.
_NOTICE_MIN_INTERVAL_S = 6 * 3600
# The notice content prefix — also the dedupe key for the min-interval check.
_NOTICE_PREFIX = "Page recovery:"
# Only these statuses can act on a recovery notice (same set as the TTL
# reaper); a terminated agent's rows are closed by its terminate trigger
# anyway, and a recovery notice must never resurrect one.
_NOTIFIABLE_STATUSES = ("running", "idling")


def _server_listening(host: str, port: int) -> bool:
    """Whether something accepts TCP on (host, port) — the liveness probe.

    A show() server is any HTTP server the agent started itself; the only
    contract is "something listens", so a TCP connect is the probe (an HTTP
    GET could 404 on a server that is otherwise fine).
    """
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _open_show_rows(conn: psycopg.Connection, host: str) -> list[tuple[int, str, int]]:
    """(agent_id, name, port) of every open show() row registered on ``host``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, name, port FROM agent_pages "
            "WHERE closed_at IS NULL AND expired_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) "
            "AND serve_dir IS NULL AND host = %s "
            "ORDER BY agent_id, name",
            (host,),
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def _recently_notified(conn: psycopg.Connection, agent_id: int) -> bool:
    """Whether this agent already got a page-recovery notice within the interval."""
    cutoff = datetime.now(UTC) - timedelta(seconds=_NOTICE_MIN_INTERVAL_S)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND source = 'system' "
            "AND content LIKE %s AND created_at > %s LIMIT 1",
            (agent_id, _NOTICE_PREFIX + "%", cutoff),
        )
        return cur.fetchone() is not None


def _agent_notifiable(conn: psycopg.Connection, agent_id: int) -> bool:
    """Whether the owner can act on a notice (running/idling only)."""
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    return row is not None and row[0] in _NOTIFIABLE_STATUSES


def _notify_owner(conn: psycopg.Connection, agent_id: int, names: list[str]) -> None:
    """One system-sourced inbound telling the owner which pages to re-serve."""
    content = (
        f"{_NOTICE_PREFIX} page(s) {', '.join(repr(n) for n in names)} of agent "
        f"{agent_id} are no longer being served after a host restart. Re-serve "
        "them with ava.ui.show() to republish."
    )
    insert_inbound_message(conn, agent_id, content, source="system")


def notify_stale_show_owners_blocking(pool: ConnectionPool, host: str) -> list[int]:
    """Scan + notify in one DB pass; returns the notified agent ids.

    Runs on one connection (via to_thread — the gateway event loop never
    blocks on psycopg or on probes). Best-effort per agent: one agent's
    failure does not abort the pass.
    """
    notified: list[int] = []
    with pool.connection() as conn:
        rows = _open_show_rows(conn, host)
        dead_by_agent: dict[int, list[str]] = {}
        for agent_id, name, port in rows:
            if not _server_listening(host, port):
                dead_by_agent.setdefault(agent_id, []).append(name)
        for agent_id, names in dead_by_agent.items():
            try:
                if not _agent_notifiable(conn, agent_id):
                    continue
                if _recently_notified(conn, agent_id):
                    continue
                _notify_owner(conn, agent_id, names)
                notified.append(agent_id)
            except Exception:
                # Roll back the aborted transaction so the next agent's work
                # does not inherit an InFailedSqlTransaction state.
                conn.rollback()
                _log.warning("[page-recovery] notify owner %s failed", agent_id, exc_info=True)
    return notified


async def run_show_page_recovery(pool: ConnectionPool, host: str) -> None:
    """One-shot startup scan — never raises (fail-open, like the TTL reaper)."""
    try:
        notified = await asyncio.to_thread(notify_stale_show_owners_blocking, pool, host)
    except Exception:
        _log.warning("[page-recovery] startup scan failed", exc_info=True)
        return
    if notified:
        logger.bind(event="page_recovery_notified", host=host).info(
            "[page-recovery] notified owners of {count} dead show page(s): {agents}",
            count=len(notified),
            agents=notified,
        )


def start_show_page_recovery(app: FastAPI) -> None:
    """Start the one-shot recovery scan from the gateway lifespan.

    Fails open: a scan failure must never take the gateway down with it.
    """
    try:
        local_host = reachable_host()
    except Exception:
        _log.warning("page-recovery scan skipped: no reachable host", exc_info=True)
    else:
        app.state.page_recovery_task = asyncio.create_task(
            run_show_page_recovery(app.state.db_pool, local_host)
        )


async def stop_show_page_recovery(app: FastAPI) -> None:
    """Cancel the recovery scan if it is still running at teardown."""
    task = getattr(app.state, "page_recovery_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
