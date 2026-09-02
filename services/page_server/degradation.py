"""Unavailable page serve-directory degradation and auto-close lifecycle."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from shared import telemetry
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.live_events import PageClosed
from shared.redis_client import publish_best_effort

_log = logging.getLogger("services.page_server.daemon")

_MISSING_SERVE_DIR_BACKOFF_BASE_S = 30.0
_MISSING_SERVE_DIR_BACKOFF_CAP_S = 300.0
_MISSING_SERVE_DIR_CLOSE_AFTER_OBSERVATIONS = 5


@dataclass
class _PageRow:
    id: int
    agent_id: int
    name: str
    port: int
    host: str
    serve_dir: str
    server_token: str | None = None
    session_name: str | None = None


@dataclass
class _DegradedServeDir:
    """Consecutive unavailable-directory observations for one page row."""

    observations: int
    retry_at: float


def _close_row(pool: ConnectionPool, row_id: int) -> bool:
    """Close one still-open page row, returning whether this call changed it."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL",
            (row_id,),
        )
        return cur.rowcount == 1


def _missing_serve_dir_backoff_s(observations: int) -> float:
    """Retry delay for an unavailable page directory observation count."""
    return min(
        _MISSING_SERVE_DIR_BACKOFF_BASE_S * (2 ** (observations - 1)),
        _MISSING_SERVE_DIR_BACKOFF_CAP_S,
    )


def _emit_missing_serve_dir(row: _PageRow, key: tuple[int, str]) -> None:
    """Record the page-directory alert; telemetry delivery itself is best-effort."""
    with suppress(Exception):
        telemetry.emit(
            "log",
            "page_serve_dir_missing",
            level="warning",
            agent_id=row.agent_id,
            attributes={
                "agent_id": row.agent_id,
                "key": f"{key[0]}:{key[1]}",
                "name": row.name,
                "serve_dir": row.serve_dir,
                "port": row.port,
            },
        )


def _publish_page_closed(row: _PageRow) -> None:
    """Best-effort PageClosed publication from this sync daemon worker."""
    event = PageClosed(agent_id=row.agent_id, name=row.name)
    with suppress(Exception):
        asyncio.run(
            publish_best_effort(
                settings.data_plane.events_channel,
                event.model_dump_json(),
                context="page_server_serve_dir_missing",
            )
        )


def _reconcile_serve_dir(
    pool: ConnectionPool,
    row: _PageRow,
    key: tuple[int, str],
    degraded: dict[tuple[int, str], _DegradedServeDir],
    backoff: dict[tuple[int, str], float],
    now: float,
) -> bool:
    """Update missing-directory state; return whether spawning must be skipped."""
    state = degraded.get(key)
    if Path(row.serve_dir).is_dir():
        if state is not None:
            _log.info("[page-server] recovered %s: serve_dir is available again", key)
            degraded.pop(key, None)
        return False
    if state is not None and now < state.retry_at:
        return True
    observations = (state.observations if state is not None else 0) + 1
    degraded[key] = _DegradedServeDir(
        observations=observations,
        retry_at=now + _missing_serve_dir_backoff_s(observations),
    )
    if state is None:
        _log.warning(
            "[page-server] degrading %s: serve_dir is missing or not a directory: %s",
            key,
            row.serve_dir,
        )
        _emit_missing_serve_dir(row, key)
    if observations >= _MISSING_SERVE_DIR_CLOSE_AFTER_OBSERVATIONS:
        if _close_row(pool, row.id):
            _publish_page_closed(row)
            _emit_missing_serve_dir(row, key)
            _log.warning(
                "[page-server] auto-closed %s: serve_dir remained unavailable after "
                "%s observations: %s",
                key,
                observations,
                row.serve_dir,
            )
        degraded.pop(key, None)
        backoff.pop(key, None)
    return True


def _discard_gone_degraded(
    degraded: dict[tuple[int, str], _DegradedServeDir], wanted: dict[tuple[int, str], _PageRow]
) -> None:
    """Forget unavailable-directory state once its page row is no longer open."""
    for key in list(degraded):
        if key not in wanted:
            del degraded[key]
