"""Postgres connection policy, the `Database` handle and the agent-table primitives.

`base/` is the UI/kernel boundary layer — `ui/*.py` does not import
`agent.*`; the two sides couple only via DB + Redis. This package
centralizes the SQL helpers used by both ends.

This module contains **pure SQL, no business semantics, used by both
ends** helpers. Connection policy and pool construction live in
`base/db/connections.py` and remain re-exported here. The inbound-message
writes, wake publish and live-agent reads are owned by `base/agents/inbox/`;
kernel-only (inbound claim, wait/mark/revert) is in `agent/db/__init__.py`.
"""

from typing import Any

import psycopg

from base.db.config import DbConfig as DbConfig
from base.db.connections import PG_KEEPALIVE_KWARGS as PG_KEEPALIVE_KWARGS
from base.db.connections import (
    PG_STATEMENT_TIMEOUT_SET_SQL as PG_STATEMENT_TIMEOUT_SET_SQL,
)
from base.db.connections import NoDatabaseAuthorityError as NoDatabaseAuthorityError
from base.db.connections import PlaceholderDbUrlError as PlaceholderDbUrlError
from base.db.connections import async_pool as async_pool
from base.db.connections import connect as connect
from base.db.connections import connect_url as connect_url
from base.db.connections import pool as pool
from base.db.handle import Database as Database


def fetch_one(cur: psycopg.Cursor, context: str) -> tuple[Any, ...]:
    """After `fetchone()`, assert there was a row — for
    `INSERT ... RETURNING` / aggregate queries where SQL contractually
    guarantees "exactly one row". `python -O` swallows assert, so we
    explicitly raise.

    `context` is a short tag from the caller (e.g. `"insert agent-1"`)
    — call sites are thin wrappers; the traceback pointing at the
    helper alone cannot tell which query failed; reading `cur.query`
    in the helper layer relies on unstable private API. In tests
    `assert` is fine; `python -O` does not run tests."""
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"expected exactly one row: {context}")
    return row


def create_agent(db: psycopg.Connection) -> int:
    """Create a new agent (agents + agents_meta row). label left NULL
    ("not set" semantics) — frontend fallback `#N`.

    The `agents` table name is constrained by LangGraph wire
    (`config["configurable"]["thread_id"]`); the public API of this
    function is "create agent". Also INSERTs the agents_meta row —
    per-agent counters for shell/monitor/schedule depend on the
    agents_meta row existing. The spawn path (gateway POST
    /api/keyed/v1/agents with prompt) uses a BackgroundTask to LLM-generate a
    short name in the background and CAS-write; non-spawn paths
    (this function, eval callers) do not auto-name; the caller is
    expected to PATCH /api/agents/{id} manually as needed.
    """
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        new_id = fetch_one(cur, "insert new agent")[0]
    db.commit()
    return new_id


def agent_exists(db: psycopg.Connection, agent_id: int) -> bool:
    """Check whether an agent exists. Web endpoints use this as a 404 precondition."""
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
        return cur.fetchone() is not None


def list_agents(db: psycopg.Connection) -> list[tuple[int, str | None]]:
    """Return all agents: (id, label). label None means "not set" —
    frontend fallback shows `#N` (see db/schema.sql `agents.label` comment)."""
    with db.cursor() as cur:
        cur.execute("SELECT id, label FROM agents ORDER BY id ASC")
        return cur.fetchall()
