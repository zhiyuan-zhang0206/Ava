"""`plugin_stats` — the runtime values behind declared statistics-panel cards.

A plugin's stat cards are declared in its manifest (`contributions.ui.stats`,
validated by `shared/plugin_ui_contributions.py`): identity and label only.
The value is runtime data — written by the plugin's own code (an agent-process
refresh hook, a daemon, a manual run) through `upsert()` here, keyed by
`(plugin, id)`, and read into `GET /api/stats/dashboard`, where the console
joins it against the declaration.

Declared-but-valueless is a legitimate state: a declared card with no row
renders as an explicit empty state, which is what lets the card exist before a
credential or a first refresh does. Failure, by contrast, is not silence: a
refresh that cannot read its source writes `status="error"` with the reason in
`detail`, and a value that stops being refreshed keeps its row so `updated_at`
ages visibly in the panel.

Writers need INSERT/UPDATE/SELECT on the table (the runner grant lives in the
plugin-stats migration and in `shared/cluster/provision.py`); the gateway
reads with its own (owner) connection.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from shared.db_transaction import write_transaction

# The status vocabulary. There is deliberately no "empty": a card with no row
# IS the empty state, and a second spelling of it would be a second fact.
STATUSES = ("ok", "warn", "error")

# Display text, not storage text: the console shows these verbatim in a
# sidebar card, so the caps are what keep one runaway error message from
# becoming the panel. `value` stays glanceable; `detail` carries the rest.
MAX_PLUGIN_CHARS = 64
MAX_ID_CHARS = 64
MAX_VALUE_CHARS = 120
MAX_DETAIL_CHARS = 500
MAX_UPDATED_BY_CHARS = 64

_UPSERT = """
INSERT INTO plugin_stats (plugin, id, value, detail, status, updated_at, updated_by)
VALUES (%s, %s, %s, %s, %s, now(), %s)
ON CONFLICT (plugin, id) DO UPDATE SET
    value = EXCLUDED.value,
    detail = EXCLUDED.detail,
    status = EXCLUDED.status,
    updated_at = now(),
    updated_by = EXCLUDED.updated_by
"""

_SELECT_ALL = (
    "SELECT plugin, id, value, detail, status, updated_at, updated_by "
    "FROM plugin_stats ORDER BY plugin, id"
)


@dataclass(frozen=True)
class PluginStatRow:
    """One card's current value, as read for the dashboard response.

    `id` is the declared card id the console joins on; a row whose id matches
    no declared card is simply never rendered.
    """

    plugin: str
    id: str
    value: str
    detail: str | None
    status: str
    updated_at: datetime
    updated_by: str | None


def _text(value: object, what: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"plugin_stats {what}: expected a non-empty string; got {value!r}")
    if len(value) > limit:
        raise ValueError(
            f"plugin_stats {what}: {len(value)} characters exceeds the {limit}-character cap"
        )
    return value


def upsert(
    *,
    plugin: str,
    id: str,
    value: str,
    detail: str | None = None,
    status: str = "ok",
    updated_by: str | None = None,
    pool: ConnectionPool[Any] | None = None,
) -> None:
    """Write (or refresh) one card's value — last write wins.

    `id` must be the id of a card the plugin declares in its manifest; the
    writer cannot check that (the manifest is not its input), so the join in
    the console is the authority and a mismatched id renders as nothing.

    `updated_by` is the machine the value was read on — meaningful in a fleet
    where more than one host may write the same card.

    `pool` (optional) borrows from a caller-held pool instead of resolving
    this process's own connection; agent-process callers omit it.
    """
    name = _text(plugin, "plugin", limit=MAX_PLUGIN_CHARS)
    card = _text(id, "id", limit=MAX_ID_CHARS)
    shown = _text(value, "value", limit=MAX_VALUE_CHARS)
    if detail is not None:
        detail = _text(detail, "detail", limit=MAX_DETAIL_CHARS)
    if status not in STATUSES:
        raise ValueError(f"plugin_stats status: {status!r} is not one of {', '.join(STATUSES)}")
    if updated_by is not None:
        updated_by = _text(updated_by, "updated_by", limit=MAX_UPDATED_BY_CHARS)

    with write_transaction(pool) as conn:
        conn.execute(_UPSERT, (name, card, shown, detail, status, updated_by))


@contextmanager
def _read_connection(pool: ConnectionPool[Any] | None) -> Generator[Connection[Any]]:
    """One connection for a read: the caller's pool when given, else this
    process's own connection — the writer side's freshness check runs in an
    agent process, which has no gateway pool.
    """
    if pool is not None:
        with pool.connection() as conn:
            yield conn
        return
    from shared.db import connect

    with connect() as conn:
        yield conn


def read_all(pool: ConnectionPool[Any] | None = None) -> list[PluginStatRow]:
    """Every card value, ordered by `(plugin, id)` — a stable read order.

    Unfiltered on purpose: declarations are the console's side of the join,
    so a row whose card was never declared (or whose plugin is now gone) is
    the console's to ignore, not this read's to guess about. `pool` (optional)
    is the gateway's; the writer side (an agent process checking whether its
    cards are due) omits it and reads on its own connection.
    """
    with _read_connection(pool) as conn, conn.cursor() as cur:
        cur.execute(_SELECT_ALL)
        rows = cur.fetchall()
    return [
        PluginStatRow(
            plugin=str(row[0]),
            id=str(row[1]),
            value=str(row[2]),
            detail=str(row[3]) if row[3] is not None else None,
            status=str(row[4]),
            updated_at=row[5],
            updated_by=str(row[6]) if row[6] is not None else None,
        )
        for row in rows
    ]
