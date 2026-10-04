"""The side-load decode-failure fallback keeps its traceback (task #4979).

`sideload_committed_ids` returns None when a claim-window write cannot be
decoded, so the reconcile falls back to the settled-history scan — the
warning that says why must carry the decode cause, not a lost `exc_info`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

import base.agents.history.inbound_sideload as sideload_mod
from base.agents.history.inbound_sideload import sideload_committed_ids
from services.agent_runner.agent_host.tests.test_reconcile_after_abort import (
    _insert_checkpoint,
    _insert_write,
)


async def test_sideload_decode_failure_keeps_its_traceback(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undecodable write falls back to the settled scan — and the fallback
    log keeps the decode cause (task #4979)."""
    agent = 879000005
    saver = AsyncPostgresSaver(aops_pool)
    type_tag, blob = saver.serde.dumps_typed(
        [HumanMessage(content="x", additional_kwargs={"ava_inbound_id": 7})]
    )
    checkpoint_id = "e0000000-0000-6000-8000-00000000000b"
    _insert_checkpoint(
        db_conn, agent, checkpoint_id, datetime.now(UTC).isoformat(), messages_version="v0"
    )
    _insert_checkpoint(
        db_conn,
        agent,
        "f0000000-0000-6000-8000-00000000000b",
        datetime.now(UTC).isoformat(),
        parent_id=checkpoint_id,
        messages_version="v1",
    )
    _insert_write(db_conn, agent, checkpoint_id, idx=0, type_tag=str(type_tag), blob=bytes(blob))

    def _boom(_value: Any, _ids: set[int]) -> None:
        raise RuntimeError("decode down")

    monkeypatch.setattr(sideload_mod, "_collect_inbound_ids", _boom)

    assert await sideload_committed_ids(aops_pool, saver, agent, since=datetime.now(UTC)) is None
    record = next(
        r
        for r in loguru_records
        if r["extra"].get("event") == "inbound_reconcile_sideload_fallback"
        and r["extra"].get("reason") == "decode_error"
    )
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
