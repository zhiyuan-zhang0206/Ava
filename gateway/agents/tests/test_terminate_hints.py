"""The lifecycle response survives failure of its advisory open-task read."""

from typing import NoReturn

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from gateway.agents import lifecycle
from ops.rpc_schemas import TerminateAgentRequest


@pytest.mark.asyncio
async def test_hint_read_failure_never_changes_accepted_termination(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    forwarded: list[int] = []

    async def accept(
        agent_id: int, path: str, body: dict[str, object], *, db: Database, pool: ConnectionPool
    ) -> dict[str, object]:
        assert db is database
        assert path == f"/api/agents/{agent_id}/terminate"
        assert body == TerminateAgentRequest().model_dump()
        forwarded.append(agent_id)
        return {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

    def fail_connection(*_args: object, **_kwargs: object) -> NoReturn:
        raise psycopg.OperationalError("hint read failed")

    monkeypatch.setattr(lifecycle, "forward_to_home_machine", accept)
    with database.pool(max_size=2) as pool:
        monkeypatch.setattr(pool, "connection", fail_connection)
        response = await lifecycle.terminate_agent_with_open_tasks(
            17, TerminateAgentRequest(), pool, db=database
        )
    assert forwarded == [17]
    assert response.model_dump() == {
        "status": "enqueued",
        "open_tasks": None,
        "shell_sessions": None,
    }
