"""The live-tree cost follows its actual nodes, never the unrelated archive.

The effective-model lookup's resolution order is pinned here too (task #4674).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from base.agents.observation import snapshot
from base.agents.observation.roster import _LIVE_SQL, AgentCard, list_directory, select_roster
from base.config import settings
from base.db import Database
from base.db.tests.fakes import fake_database
from base.host.env.agent_slices import ModelOverrides


def seed(conn: psycopg.Connection, rows: list[tuple[int, str, str]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO agents (id, label) VALUES (%s, %s)",
            [(r[0], f"Agent {r[0]}") for r in rows],
        )
        cur.executemany("INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)", rows)
    conn.commit()


def test_live_roster_preserves_ancestor_closure_without_history_growth(
    db_conn: psycopg.Connection,
) -> None:
    seed(
        db_conn,
        [
            (1, "user", "idling"),
            (2, "agent:1", "terminated"),
            (3, "agent:2", "running"),
            (4, "agent:2", "idling"),
        ],
    )
    before = select_roster(db_conn)
    assert [a.agent_id for a in before.agents] == [1, 3, 4]
    assert [a.model_dump() for a in before.ancestors] == [
        {"agent_id": 2, "spawner": "agent:1", "fork_source_agent_id": None}
    ]
    seed(db_conn, [(i, "user", "terminated") for i in range(100, 10_100)])
    after = select_roster(db_conn)
    before_payload = before.model_dump(mode="json")
    after_payload = after.model_dump(mode="json")
    # Roster content is stable even though each read has a new assessment time.
    for payload in (before_payload, after_payload):
        for card in payload["agents"]:
            card["availability"].pop("observed_at")
    assert after_payload == before_payload


def test_ancestor_closure_follows_fork_source_and_deduplicates_cycles(
    db_conn: psycopg.Connection,
) -> None:
    seed(
        db_conn,
        [
            (1, "agent:2", "terminated"),
            (2, "agent:1", "terminated"),
            (3, "user", "running"),
            (4, "user", "idling"),
        ],
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET fork_source_agent_id=1, fork_source_checkpoint_id='ckpt' WHERE id IN (3,4)"
        )
    roster = select_roster(db_conn)
    assert [a.agent_id for a in roster.ancestors] == [1, 2]
    assert all(a.fork_source_agent_id == 1 for a in roster.agents)


def test_directory_is_bounded_searchable_and_cursor_ordered(db_conn: psycopg.Connection) -> None:
    seed(db_conn, [(i, "user", "terminated") for i in range(1, 8)])
    first = list_directory(db_conn, scope="terminated", limit=3)
    second = list_directory(db_conn, scope="terminated", before_id=first.next_cursor, limit=3)
    third = list_directory(db_conn, scope="terminated", before_id=second.next_cursor, limit=3)
    assert [a.agent_id for a in first.agents] == [7, 6, 5]
    assert [a.agent_id for a in second.agents] == [4, 3, 2]
    assert [a.agent_id for a in third.agents] == [1]
    assert third.next_cursor is None
    assert [a.agent_id for a in list_directory(db_conn, scope="all", query="#3").agents] == [3]
    assert [a.agent_id for a in list_directory(db_conn, scope="all", query="Agent 2").agents] == [2]
    assert list_directory(db_conn).agents == []


def test_notice_body_cannot_expand_roster_card(db_conn: psycopg.Connection) -> None:
    seed(db_conn, [(1, "user", "idling")])
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id,local_id,title,content,priority,blocking,require_response,expire_at) VALUES (1,1,'question',%s,'P0',true,true,now()+interval '1 day')",
            ("x" * 1_000_000,),
        )
    card = select_roster(db_conn).agents[0]
    assert card.awaiting_response_count == 1
    assert card.highest_notice_priority == "P0"
    assert "notices_awaiting_response" not in AgentCard.model_fields
    assert len(card.model_dump_json()) < 1500


def test_open_impersonation_status_reflects_lease_phase(db_conn: psycopg.Connection) -> None:
    """Only an `active` lease means the agent is actually taken over — a
    `requested` lease still carries an open session number (so the UI can gate
    the force-expire action) but the native agent keeps running until
    activation reaches its next safe boundary."""
    seed(db_conn, [(1, "user", "idling")])
    lease_id = str(uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_impersonations"
            "(id, agent_id, source, machine, status, ttl_seconds, expires_at) "
            "VALUES (%s, 1, 'external_agent:codex', 'test-machine', 'requested', "
            "3600, clock_timestamp() + interval '1 hour')",
            (lease_id,),
        )
    db_conn.commit()

    requested_card = select_roster(db_conn).agents[0]
    assert requested_card.open_impersonation_session_id is not None
    assert requested_card.open_impersonation_status == "requested"

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_impersonations SET status = 'active', "
            "activated_at = clock_timestamp() WHERE id = %s",
            (lease_id,),
        )
    db_conn.commit()

    active_card = select_roster(db_conn).agents[0]
    assert active_card.open_impersonation_session_id == requested_card.open_impersonation_session_id
    assert active_card.open_impersonation_status == "active"


@pytest.mark.parametrize("limit", [0, 201])
def test_invalid_directory_limit_fails(db_conn: psycopg.Connection, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        list_directory(db_conn, limit=limit)


@pytest.mark.parametrize("query", ["\u00b2", "\u0661", "9" * 100, "9223372036854775808"])
def test_arbitrary_search_text_cannot_overflow_an_id(
    db_conn: psycopg.Connection, query: str
) -> None:
    seed(db_conn, [(1, "user", "idling")])
    assert list_directory(db_conn, query=query).agents == []


@pytest.mark.parametrize(
    "spawner", ["agent:" + "9" * 100, "agent:9223372036854775808", "agent:18446744073709551615"]
)
def test_arbitrary_external_spawner_does_not_break_the_roster(
    db_conn: psycopg.Connection, spawner: str
) -> None:
    seed(db_conn, [(1, spawner, "idling")])
    roster = select_roster(db_conn)
    assert roster.agents[0].spawner == spawner
    assert roster.ancestors == []


@pytest.mark.parametrize("before_id", [0, -1, 9223372036854775808, 10**100])
def test_directory_rejects_invalid_cursor(db_conn: psycopg.Connection, before_id: int) -> None:
    with pytest.raises(ValueError, match="before_id"):
        list_directory(db_conn, before_id=before_id)


def test_roster_attention_skips_resolved_and_unrelated_notices(db_conn: psycopg.Connection) -> None:
    """Count rows actually visited, without pinning PostgreSQL's chosen plan names."""
    seed(db_conn, [(90001, "user", "idling")])
    seed(db_conn, [(i, "user", "terminated") for i in range(90002, 90102)])
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agent_notices
                (agent_id,local_id,title,priority,require_response,resolved_at,resolution,expire_at)
            SELECT 90001,i,'old','P3',false,now(),'read',now()+interval '1 day'
            FROM generate_series(1,20000)i
        """)
        cur.execute("""
            INSERT INTO agent_notices
                (agent_id,local_id,title,priority,require_response,expire_at)
            VALUES (90001,20001,'new','P3',false,now()+interval '1 day'),
                   (90001,20002,'reply','P1',true,now()+interval '1 day')
        """)
        cur.execute("""
            INSERT INTO agent_notices
                (agent_id,local_id,title,priority,require_response,expire_at)
            SELECT 90002+i%100,i,'other','P3',false,now()+interval '1 day'
            FROM generate_series(1,20000)i
        """)
        cur.execute("ANALYZE agent_notices")
        cur.execute(sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) ") + _LIVE_SQL)
        row = cur.fetchone()
        assert row is not None
        plan: dict[str, Any] = row[0][0]["Plan"]

    def visited_notices(node: dict[str, Any]) -> float:
        visited = 0
        if node.get("Relation Name") == "agent_notices":
            visited = (
                node["Actual Rows"]
                + node.get("Rows Removed by Filter", 0)
                + node.get("Rows Removed by Index Recheck", 0)
            ) * node["Actual Loops"]
        return visited + sum(visited_notices(child) for child in node.get("Plans", []))

    assert visited_notices(plan) < 100


class _FakeConn:
    """A connection stand-in serving one `fetchone()` result as a context manager."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> _FakeConn:
        self.queries.append((sql, tuple(params)))
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _install_conn(row: tuple[Any, ...] | None) -> tuple[Database, _FakeConn]:
    conn = _FakeConn(row)

    def _connect(**_kwargs: object) -> _FakeConn:
        return conn

    return fake_database(_connect), conn


def test_effective_model_overlay_wins() -> None:
    db, conn = _install_conn(({"llm_model": "deepseek-v4-pro"}, {"llm_model": "deepseek-v4-flash"}))
    assert snapshot.agent_model_target(db, 42, fallback="fallback-x")[0] == "deepseek-v4-pro"
    ((sql, params),) = conn.queries
    assert "agents_meta" in sql and params == (42,)


def test_effective_model_is_the_birth_stamp_when_there_is_no_overlay() -> None:
    """`llm_model` is birth-frozen: an agent born under an older default keeps it."""
    db, _ = _install_conn(({}, {"llm_model": "deepseek-v4-pro"}))
    assert snapshot.agent_model_target(db, 42, fallback="fallback-x")[0] == "deepseek-v4-pro"


def test_effective_model_defaults_to_the_fleet_model_without_an_overlay() -> None:
    db, _ = _install_conn(({}, {}))
    assert snapshot.agent_model_target(db, 42, fallback="fallback-x")[0] == settings.lm.llm_model


def test_effective_model_defaults_to_the_fleet_model_when_the_row_vanished() -> None:
    db, _ = _install_conn(None)
    assert snapshot.agent_model_target(db, 42, fallback="fallback-x")[0] == settings.lm.llm_model


def test_effective_model_read_failure_returns_the_callers_fallback() -> None:
    def boom(**_kw: object) -> None:
        raise RuntimeError("db down")

    model, overrides = snapshot.agent_model_target(fake_database(boom), 42, fallback="fallback-x")
    assert model == "fallback-x"
    assert overrides == ModelOverrides.from_pins(None)


def test_model_target_carries_the_agents_tuning_pins() -> None:
    """The overlay's pinned effort wins over the birth stamp's; unpinned fields stay unset."""
    db, _ = _install_conn(
        ({"reasoning_effort": "max"}, {"reasoning_effort": "low", "llm_model": "deepseek-v4-pro"})
    )
    model, overrides = snapshot.agent_model_target(db, 42, fallback="fallback-x")
    assert model == "deepseek-v4-pro"
    assert overrides.reasoning_effort == "max"
    assert overrides.claude_thinking_budget_tokens is None


def test_model_target_without_pins_leaves_every_tuning_field_to_the_cluster_default() -> None:
    db, _ = _install_conn(({}, {}))
    _, overrides = snapshot.agent_model_target(db, 42, fallback="fallback-x")
    assert overrides == ModelOverrides.from_pins(None)
