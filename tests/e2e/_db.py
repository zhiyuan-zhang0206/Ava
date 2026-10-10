"""Shared e2e DB pollers — wait for an agent row to reach a target lifecycle state.

One generous default timeout in one place. A genuine hang still fails, just at
a higher ceiling. Previously each lifecycle test carried its own copy-pasted
`_wait_for_status` and the defaults had drifted (20s / 20s / 30s); this is the
single source of truth.

Why 90s: generous headroom, not a tight bound. CI runs e2e serially on a
dedicated box (one full stack at a time, `-n 1`), so a turn normally completes
in single-digit seconds; the ceiling only has to clear a transient slow
boot/turn under incidental host load without racing a tight deadline. A real
hang (agent dead, status never flips) still fails, just at a higher ceiling —
and the agent-side stall probe (AVA_NODE_STALL_DUMP_SECONDS) names the blocked frame
in the `agent-*.log` artifact either way.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import psycopg

from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.delta_read_compat import reconstruct_delta_messages
from base.config import settings
from base.db import Database
from tests.components.base.poll_until import poll_until
from tests.e2e._ports import GATEWAY_URL


def wait_for_status(agent_id: int, target: str, timeout: float = 90.0) -> None:
    """Poll agents_meta.status until it equals target, else raise after timeout."""
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        last = row[0] if row else None
        if last == target:
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"agent {agent_id} {timeout}s did not reach status={target!r} (last={last!r})"
    )


def checkpoint_values(agent_id: int) -> dict[str, Any]:
    """The agent's latest checkpoint channel values ({} before its first checkpoint)."""
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
        if tup is not None:
            # Delta write model (#3180): fold delta-written messages on read.
            reconstruct_delta_messages(saver, tup)
    if tup is None:
        return {}
    return tup.checkpoint.get("channel_values", {})  # pyright: ignore[reportUnknownMemberType]


def chat_and_wait(agent_id: int, text: str, *, timeout: float = 90.0) -> None:
    """POST one user message and wait until its inbound row is finalized and the agent idles.

    Inbound `done` (not just status idling): idling flips at claim entry, before the
    turn's work has happened.
    """
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/messages",
        json={"content": text, "source": "user"},
        timeout=10.0,
    ).raise_for_status()

    def finished() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat' "
                "AND content = %s",
                (agent_id, text),
            )
            inbound = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        status = row[0] if row else None
        return inbound == ["done"] and status == "idling", {"inbound": inbound, "agent": status}

    poll_until(finished, timeout=timeout, interval=0.3, what=f"agent {agent_id} finishes {text!r}")


def enqueue_compact_history_fixture(agent_id: int, *, database: Database) -> int:
    """Produce the native compact envelope used by history-rendering scenarios.

    This is a test fixture for the existing graph/history contract, not public
    manual-compaction admission. Product callers use observed compact-history.
    """
    from base.db import insert_compact_request_inbound
    from base.events.live.bus import EventBus

    with psycopg.connect(settings.data_plane.db_url) as conn:
        return insert_compact_request_inbound(
            conn, agent_id, database=database, bus=EventBus.from_settings()
        )
