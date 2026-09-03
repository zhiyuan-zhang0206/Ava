"""ava.ui.show / .close panel registration, gateway-internal DB layer.

The agent calls `ava.ui.show(name)` to start an HTTP server inside its own
process (bound on the runner's reachable host — loopback on a single box),
self-reporting its reachable host; this module writes the registration into
the agent_pages table. The page URL is the gateway's own
`/pages/<id>-<name>/` reverse-proxy address: the gateway forwards
GETs to the page server, so the browser never dials it directly (host:port
stays inside the gateway — it appears in no URL).

Terminalization: closed_at NULL = open, non-NULL = closed (terminal). A
UNIQUE partial index `WHERE closed_at IS NULL` ensures only one open row
per (agent, name); historical closed rows do not block re-registration.

register_page is an idempotent upsert — re-registering the same name
updates host + port (agent restarted the server on a different port /
name reuse), without colliding on the UNIQUE index. close is a CAS
UPDATE; 0 rows returns None so the caller can distinguish not-found vs
already-closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ops.rpc_schemas import PageRow
from shared.config import settings
from shared.db import fetch_one

_SELECT_COLUMNS = (
    "id, agent_id, name, port, host, title, serve_dir, created_at, closed_at, "
    "expires_at, expired_at"
)


def _page_url(agent_id: int, name: str) -> str:
    # Gateway reverse-proxy path: the gateway serves page content at
    # /pages/<agent_id>-<name>/ and the router absolutizes it against
    # the request base URL before returning. The browser never dials the
    # page server directly, so host/port appear in no URL — they stay in
    # the row for the proxy's own dialing.
    #
    # The composite key is parseable because agent_id is numeric: split on
    # the FIRST dash yields (agent_id, name) even when name itself contains
    # dashes (name charset is [a-zA-Z0-9_-]).
    return f"/pages/{agent_id}-{name}/"


def _row_to_record(row: tuple[Any, ...]) -> PageRow:
    (
        pid,
        agent_id,
        name,
        port,
        _host,
        title,
        serve_dir,
        created_at,
        closed_at,
        _expires_at,
        _expired_at,
    ) = row
    return PageRow(
        id=pid,
        agent_id=agent_id,
        name=name,
        port=port,
        title=title,
        serve_dir=serve_dir,
        url=_page_url(agent_id, name),
        created_at=created_at,
        closed_at=closed_at,
    )


def register_page(
    db: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    host: str,
    title: str | None,
    serve_dir: str | None = None,
    ttl_seconds: int | None = None,
) -> PageRow:
    """Register or update an open page.

    If an open row with the same name exists -> UPDATE host + port + title +
    serve_dir (agent restarted server on different port). None -> INSERT.
    Returns the final row snapshot. The caller is responsible for
    publishing the PageOpened event.

    `serve_dir` is the directory the page server serves, recorded by
    ava.ui.serve() so agent boot can re-serve a dead page
    server after resurrect/restart; NULL for ava.ui.show() pages (the agent
    manages those servers itself).
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.daemon.page_default_ttl_seconds
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET port = %s, host = %s, title = %s, serve_dir = %s, "  # noqa: S608
            "expires_at = %s, expired_at = NULL "
            "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
            "RETURNING " + _SELECT_COLUMNS,
            (port, host, title, serve_dir, expires_at, agent_id, name),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO agent_pages "
                "(agent_id, name, port, host, title, serve_dir, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING " + _SELECT_COLUMNS,
                (agent_id, name, port, host, title, serve_dir, expires_at),
            )
            row = fetch_one(cur, f"insert page agent={agent_id} name={name!r}")
    db.commit()
    return _row_to_record(row)


def close_page(
    db: psycopg.Connection,
    agent_id: int,
    name: str,
) -> PageRow | None:
    """CAS open->closed. Returns None for 0 rows (caller distinguishes not-found / already-closed)."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() "  # noqa: S608
            "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
            "RETURNING " + _SELECT_COLUMNS,
            (agent_id, name),
        )
        row = cur.fetchone()
    db.commit()
    return _row_to_record(row) if row is not None else None


def get_open_page_target(
    db: psycopg.Connection,
    agent_id: int,
    name: str,
) -> tuple[str, int] | None:
    """(host, port) of one currently open page — the reverse-proxy dial
    target. None for unknown agent / unknown name / closed page (the proxy
    404s on all three; callers do not need to distinguish).

    Returns the bare host:port instead of a PageRow: the wire schema carries
    no host (it stays inside the gateway), so the proxy reads it here."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT host, port FROM agent_pages "
            "WHERE agent_id = %s AND name = %s "
            "AND closed_at IS NULL AND expired_at IS NULL",
            (agent_id, name),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row is not None else None


def list_open_pages(db: psycopg.Connection, agent_id: int) -> list[PageRow]:
    """Main frontend page-load entry: fetch the agent's currently open page list."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT " + _SELECT_COLUMNS + " FROM agent_pages "  # noqa: S608
            "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "ORDER BY created_at ASC",
            (agent_id,),
        )
        rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def list_all_open_pages(db: psycopg.Connection) -> list[PageRow]:
    """Fleet view page-load: every agent's currently open pages in one fetch
    (the per-agent GET would be N requests across the fleet). SSE
    PageOpened/PageClosed handles deltas after load."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT " + _SELECT_COLUMNS + " FROM agent_pages "  # noqa: S608
            "WHERE closed_at IS NULL AND expired_at IS NULL ORDER BY created_at ASC",
        )
        rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def close_all_agent_pages(
    db: psycopg.Connection,
    agent_id: int,
) -> list[str]:
    """Close every currently open page for ``agent_id``.

    Returns the names of the closed pages so the caller can publish
    PageClosed events for each one (every close needs its own event for
    the frontend to remove the entry from the popover).

    Unlike ``close_page``, this is not a CAS on a single name — it closes
    all open rows in one UPDATE, which is safe because the UNIQUE partial
    index already limits an agent to one open row per name, and the
    single-page-per-agent convention (enforced by the SDK and the router)
    guarantees at most one open row at a time in normal operation. A
    bulk-close is still needed for the terminate cascade and for the
    migration path where pre-existing agents had multiple pages.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT name FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL",
            (agent_id,),
        )
        names = [r[0] for r in cur.fetchall()]
        if names:
            cur.execute(
                "UPDATE agent_pages SET closed_at = now() "
                "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL",
                (agent_id,),
            )
    db.commit()
    return names


def list_open_page_names(db: psycopg.Connection, agent_id: int) -> list[str]:
    """Return the pages a terminate cascade will close.

    Daemon-supervised serve() pages have a ``serve_dir`` and remain open across
    termination; only agent-owned show() pages are captured for PageClosed
    events. After the caller UPDATEs agents_meta.status='terminated', the SQL
    trigger silently closes these rows and the frontend removes them in real time.

    Does not return full PageRow — publishing only needs name (PageClosed
    fields are only agent_id + name). Separate SELECT to avoid the
    url-composition in _row_to_record (not needed by the terminate
    path)."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT name FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NULL",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]
