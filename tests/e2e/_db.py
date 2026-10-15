from __future__ import annotations

import os
import time
from typing import Any

import httpx
import psycopg
from psycopg import sql

from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.delta_read_compat import reconstruct_delta_messages
from base.config import settings
from base.db import Database
from base.sessions.tests.poll_until import poll_until
from tests.e2e._ports import GATEWAY_URL


def wait_for_status(agent_id: int, target: str, timeout: float = 90.0) -> None:
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
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
        if tup is not None:
            reconstruct_delta_messages(saver, tup)
    if tup is None:
        return {}
    return tup.checkpoint.get("channel_values", {})


def chat_and_wait(agent_id: int, text: str, *, timeout: float = 90.0) -> None:
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
    from base.agents.inbox.compact import insert_compact_request_inbound
    from base.events.live.bus import EventBus

    with psycopg.connect(settings.data_plane.db_url) as conn:
        return insert_compact_request_inbound(
            conn, agent_id, database=database, bus=EventBus.from_settings()
        )


def apply_e2e_seq_offset() -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER SEQUENCE agents_id_seq RESTART {}").format(
                sql.Literal(os.getpid() * 1000)
            )
        )
