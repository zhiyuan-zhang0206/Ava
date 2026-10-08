"""Close the show() pages whose server died, a loop of the page-server service.

A `show()` page (no `serve_dir`) is served by the agent's own process, so the
page-server daemon does not supervise it, and a dead server cannot be rebuilt: the
row is closed so the dead link stops showing as open, the frontend drops it on the
`PageClosed` event, and the owner gets one system inbound telling it to re-show the
page (deduped per agent over six hours, `base/agents/recovery/pages.py`). The agent
runs the same pass at boot and on each heartbeat, but a busy agent gets no
heartbeats (task #2260), so this loop scans every open show page of this host every
`AVA_HEARTBEAT_INTERVAL_SECONDS` as well.

`serve()` pages need nothing here: the daemon's own reconcile relaunches a dead page
server in its persistent session within one poll.

A round skips while the unit is quiesced (an `ava stop` draining). An unreachable
database skips the round; any other exception ends the loop and, through the
service's `TaskGroup`, the process.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg_pool import ConnectionPool

from base.agents.recovery import pages as page_recovery
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database, publish_inbound_wake
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
    """Close the agent's dead show() rows and tell it to re-show them; True when a
    notice was written.

    Close and notice are ONE transaction, so a failure rolls back both and the next
    round retries: the agent is never told about rows that stayed open. The notice is
    deduped over `MIN_INTERVAL_S` against the agent's own inbound history.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=page_recovery.MIN_INTERVAL_S)
    with write_transaction(pool) as conn, conn.cursor() as cur:
        for name in names:
            cur.execute(page_recovery.CLOSE_PAGE_SQL, (agent_id, name))
        cur.execute(
            page_recovery.RECENT_NOTICE_SQL,
            (agent_id, page_recovery.NOTICE_PREFIX + "%", cutoff),
        )
        if cur.fetchone() is not None:
            return False
        cur.execute(
            page_recovery.NOTICE_INSERT_SQL,
            (agent_id, page_recovery.recovery_notice(agent_id, names)),
        )
    return True


async def dead_pages_round(
    pool: ConnectionPool, db: Database, host: str, progress: LoopProgress, bus: EventBus
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
        notified = await asyncio.to_thread(close_dead_show_pages, pool, agent_id, names)
        for name in names:
            await bus.publish_best_effort(
                PageClosed(agent_id=agent_id, name=name).model_dump_json(),
                context="page_server_dead_show",
            )
        _log.warning(
            "[page-server] closed dead show page(s) %s of agent %s (owner %s)",
            names,
            agent_id,
            "told to re-show them" if notified else "already told",
        )
        if notified:
            # Wake the agent so the notice is claimed promptly (its claim loop's
            # SELECT recheck delivers it within its timeout regardless).
            await asyncio.to_thread(publish_inbound_wake, db, bus, agent_id, "0")
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
