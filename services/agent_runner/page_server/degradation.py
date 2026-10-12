"""Page snapshots and terminal unavailable-link observation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.events.live.projection import PageClosed


@dataclass
class _PageRow:
    id: int
    agent_id: int
    name: str
    port: int
    host: str
    serve_dir: str
    server_token: str | None = field(default=None, repr=False)
    session_name: str | None = None


def _close_failed_page(pool: ConnectionPool, row: _PageRow, bus: EventBus) -> None:
    """Close only this snapshot; a fresh explicit registration stays untouched."""
    with write_transaction(pool) as conn:
        changed = conn.execute(
            "UPDATE agent_pages SET closed_at = now() WHERE id = %s AND port = %s "
            "AND serve_dir = %s AND server_token IS NOT DISTINCT FROM %s AND host = %s "
            "AND closed_at IS NULL AND expired_at IS NULL",
            (row.id, row.port, row.serve_dir, row.server_token, row.host),
        ).rowcount
    if changed:
        asyncio.run(
            bus.publish_best_effort(
                PageClosed(agent_id=row.agent_id, name=row.name).model_dump_json(),
                context="page_server_unavailable",
            )
        )
