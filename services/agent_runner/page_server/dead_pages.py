"""Observe dead show() links and close their rows without agent recovery messages."""

from __future__ import annotations

import asyncio
import functools
import logging
import urllib.request
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.events.live.projection import PageClosed
from services.agent_runner.page_server.config import PageServerConfig

_log = logging.getLogger("services.agent_runner.page_server.dead_pages")

_PROBE_TIMEOUT_S = 1.5
# Pages probed at once within a round: each probe is a blocking HTTP call in a thread.
_PROBE_CONCURRENCY = 8


@dataclass(frozen=True)
class ShowPage:
    """One open show() page of this host."""

    agent_id: int
    name: str
    port: int
    host: str


def open_show_pages(pool: ConnectionPool, host: str) -> list[ShowPage]:
    """Every open page of this host that has no serve_dir (the agent's own server)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, name, port, host FROM agent_pages "
            "WHERE host = %s AND serve_dir IS NULL "
            "AND closed_at IS NULL AND expired_at IS NULL "
            "ORDER BY agent_id, name",
            (host,),
        )
        return [ShowPage(*row) for row in cur.fetchall()]


def page_server_alive(host: str, port: int) -> bool:
    """Probe a page server's /health endpoint: True when any 200 answers."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/health", timeout=_PROBE_TIMEOUT_S
        ) as resp:
            return resp.status == 200
    except OSError:
        return False


def close_dead_show_pages(pool: ConnectionPool, agent_id: int, names: list[str]) -> bool:
    """Close invalid links. There is no recovery notice or model wake."""
    changed = False
    with write_transaction(pool) as conn:
        for name in names:
            changed |= (
                conn.execute(
                    "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s "
                    "AND name = %s AND serve_dir IS NULL AND closed_at IS NULL AND expired_at IS NULL",
                    (agent_id, name),
                ).rowcount
                == 1
            )
    return changed


async def dead_pages_round(
    pool: ConnectionPool, _db: Database, host: str, progress: LoopProgress, bus: EventBus
) -> None:
    """Probe every open show page of this host and close the dead ones."""
    if admission.quiesced():
        return
    pages = await asyncio.to_thread(open_show_pages, pool, host)
    dead: dict[int, list[ShowPage]] = {}

    async def probe(page: ShowPage) -> None:
        if not await asyncio.to_thread(page_server_alive, page.host, page.port):
            dead.setdefault(page.agent_id, []).append(page)

    await round_loop.fan_out(
        [functools.partial(probe, page) for page in pages],
        concurrency=_PROBE_CONCURRENCY,
        progress=progress,
    )
    for agent_id, dead_pages in sorted(dead.items()):
        names = [page.name for page in dead_pages]
        await asyncio.to_thread(close_dead_show_pages, pool, agent_id, names)
        for name in names:
            await bus.publish_best_effort(
                PageClosed(agent_id=agent_id, name=name).model_dump_json(),
                context="page_server_dead_show",
            )
        _log.warning("[page-server] closed dead show page(s) %s of agent %s", names, agent_id)
        progress.beat()


async def dead_pages_loop(
    pool: ConnectionPool,
    db: Database,
    host: str,
    progress: LoopProgress,
    config: PageServerConfig,
    bus: EventBus,
) -> None:
    """The dead-show-page scan as a resident sequential loop: one round at start,
    then every heartbeat interval."""
    interval_s = float(config.heartbeat_interval_seconds)

    async def one_round() -> None:
        await dead_pages_round(pool, db, host, progress, bus)

    await round_loop.run_rounds("dead-show-pages", progress, interval_s, one_round)
