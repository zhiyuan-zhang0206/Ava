# meiva:ref base/agents/history/docs/range-read-contracts.meiva.okf.md#history-range-inputs
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection, sql

from base.agents.history.hierarchy.usage import GenerationUsage
from base.db import Database

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredNode:
    id: int
    depth: int
    span_start: int
    span_end: int
    start_ts: datetime | None
    end_ts: datetime | None
    text: str
    parent_id: int | None
    engine_version: str
    prompt_version: str
    job_id: int | None = None
    check_key: str | None = None


@contextmanager
def _read_connection(db: Database) -> Generator[Connection[Any]]:
    db_pool = db.pool(autocommit=True)
    try:
        with db_pool.connection() as conn:
            yield conn
    finally:
        db_pool.close()


def load_nodes(db: Database, agent_id: int) -> list[StoredNode]:
    with _read_connection(db) as conn:
        rows = conn.execute(
            """
            SELECT id, depth, span_start, span_end, start_ts, end_ts, text, parent_id,
                   engine_version, prompt_version, job_id, check_key
            FROM understanding_nodes
            WHERE agent_id = %s
            ORDER BY depth, span_start
            """,
            (agent_id,),
        ).fetchall()
    return [StoredNode(*row) for row in rows]


def _costs(rows: list[Any]) -> dict[Any, GenerationUsage]:
    return {
        key: GenerationUsage(int(calls), int(inp), int(cache), int(out), float(ms) / 1000)
        for key, calls, inp, cache, out, ms in rows
    }


def load_generation_costs(
    db: Database,
    agent_id: int,
    *,
    job_ids: list[int] | None = None,
    check_keys: list[str] | None = None,
) -> tuple[dict[int, GenerationUsage], dict[str, GenerationUsage]]:
    with _read_connection(db) as conn:
        jobs = conn.execute(
            sql.SQL("""
            SELECT job_id, count(*), coalesce(sum((usage_metadata->>'input_tokens')::bigint), 0),
                   coalesce(sum((usage_metadata->'input_token_details'->>'cache_read')::bigint), 0),
                   coalesce(sum((usage_metadata->>'output_tokens')::bigint), 0), sum(duration_ms)
            FROM understanding_chunk_calls WHERE agent_id = %s {} GROUP BY job_id
            """).format(sql.SQL(" AND job_id = ANY(%s)" if job_ids is not None else "")),
            (agent_id, job_ids) if job_ids is not None else (agent_id,),
        ).fetchall()
        checks = conn.execute(
            sql.SQL("""
            SELECT check_key, count(*), coalesce(sum((usage_metadata->>'input_tokens')::bigint), 0),
                   coalesce(sum((usage_metadata->'input_token_details'->>'cache_read')::bigint), 0),
                   coalesce(sum((usage_metadata->>'output_tokens')::bigint), 0), sum(duration_ms)
            FROM understanding_group_calls WHERE agent_id = %s {} GROUP BY check_key
            """).format(sql.SQL(" AND check_key = ANY(%s)" if check_keys is not None else "")),
            (agent_id, check_keys) if check_keys is not None else (agent_id,),
        ).fetchall()
    return _costs(jobs), _costs(checks)
