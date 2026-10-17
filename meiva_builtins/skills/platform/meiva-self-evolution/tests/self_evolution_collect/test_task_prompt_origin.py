from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.db import Database
from meiva_builtins.skills.tests.skill_script import load_skill_script


@pytest.fixture(scope="module")
def collect_mod() -> Any:
    return load_skill_script("platform", "meiva-self-evolution", "scripts", "collect.py")


def _insert_agent(cur: psycopg.Cursor, agent_id: int, spawner: str = "user") -> None:
    cur.execute(
        "INSERT INTO agents (id, label) VALUES (%s, %s)",
        (agent_id, f"test-agent-{agent_id}"),
    )
    cur.execute(
        "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, 'idling')",
        (agent_id, spawner),
    )


def _insert_inbound(
    cur: psycopg.Cursor, agent_id: int, source: str, content: str, age: str, *, kind: str = "chat"
) -> None:
    cur.execute(
        "INSERT INTO inbound_messages (agent_id, source, content, kind, created_at) "
        "VALUES (%s, %s, %s, %s, now() - %s::interval)",
        (agent_id, source, content, kind, age),
    )


def _fetch(collect_mod: Any, cur: psycopg.Cursor, agent_ids: list[int]) -> Any:
    now = datetime.now(UTC)
    return collect_mod._inbounds_by_agent(cur, agent_ids, now - timedelta(days=1), now)


def _build(
    collect_mod: Any,
    database: Database,
    agent_id: int,
    inbounds: list[dict[str, Any]],
    spawner: str,
    *,
    transcript: list[dict[str, str]] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    return collect_mod.build_record(
        database=database,
        agent_id=agent_id,
        week="2026-08-30",
        events=[],
        log_events=[],
        inbounds=inbounds,
        transcript=[] if transcript is None else transcript,
        meta=(spawner, status, "delivered"),
    )


def test_spawn_brief_older_than_the_window_is_the_task_prompt(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 8, spawner="agent:1")
        _insert_inbound(cur, 8, "agent:1", "ancient spawn brief", "10 days")
    db_conn.commit()

    with db_conn.cursor() as cur:
        inbounds = _fetch(collect_mod, cur, [8])

    rec = _build(collect_mod, database, 8, inbounds[8], "agent:1")

    assert rec["task_prompt"] == "ancient spawn brief"
    assert rec["task_prompt_source"] == "spawner"


def test_shell_notice_does_not_shadow_the_spawn_brief(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 10, spawner="agent:1")
        _insert_inbound(
            cur, 10, "shell:0", "Background command 'build' exited with code 0", "3 hours"
        )
        _insert_inbound(cur, 10, "agent:1", "spawn brief", "2 days")
    db_conn.commit()

    with db_conn.cursor() as cur:
        inbounds = _fetch(collect_mod, cur, [10])

    rec = _build(collect_mod, database, 10, inbounds[10], "agent:1")

    assert rec["task_prompt"] == "spawn brief"
    assert rec["task_prompt_source"] == "spawner"


def test_shell_session_notice_is_never_the_task_prompt(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 11, spawner="agent:1")
        _insert_inbound(
            cur, 11, "system", "Shell session 'w' was reclaimed after its TTL expired", "1 hour"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        inbounds = _fetch(collect_mod, cur, [11])

    rec = _build(collect_mod, database, 11, inbounds.get(11, []), "agent:1")

    assert rec["task_prompt"] == ""
    assert rec["task_prompt_source"] == ""


def test_resurrect_wake_contributes_no_task_inbound(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 12, spawner="agent:1")
        _insert_inbound(cur, 12, "user", "", "1 hour", kind="resurrect")
    db_conn.commit()

    with db_conn.cursor() as cur:
        inbounds = _fetch(collect_mod, cur, [12])

    rec = _build(collect_mod, database, 12, inbounds.get(12, []), "agent:1", status="terminated")

    assert rec["task_prompt"] == ""


def test_window_bounds_come_from_the_collection_window(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 13, spawner="agent:1")
        _insert_inbound(cur, 13, "agent:1", "task brief", "11 days")
        _insert_inbound(cur, 13, "agent:9", "peer ping from another window", "2 days")
    db_conn.commit()

    now = datetime.now(UTC)
    with db_conn.cursor() as cur:
        out = collect_mod._inbounds_by_agent(
            cur, [13], now - timedelta(days=13), now - timedelta(days=12)
        )

    assert [message["content"] for message in out[13]] == ["task brief"]
