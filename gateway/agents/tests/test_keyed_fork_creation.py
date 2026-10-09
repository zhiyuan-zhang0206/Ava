"""A fork recovers its committed child rather than copying a later source state."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from ava.gateway_client import transport
from base.agents import GatewayUnavailable
from gateway.agents import router as agent_router
from gateway.agents.tests.test_guarded_creation import HEADERS, PATH
from gateway.agents.tests.test_guarded_creation import client as client
from gateway.agents.tests.test_sdk_strong_creation import _response, _sdk
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process


def _checkpoint(conn: psycopg.Connection, agent_id: int, checkpoint_id: str) -> None:
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata) "
        "VALUES (%s,%s,%s,%s)",
        (str(agent_id), checkpoint_id, Jsonb({"channel_versions": {}}), Jsonb({})),
    )
    conn.commit()


@pytest.mark.parametrize("source_change", ["advanced", "empty"])
def test_sdk_fork_lost_response_retains_original_child_and_checkpoint(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    source_change: str,
) -> None:
    parent = client.post(
        PATH, json={"machine": "local-test"}, headers={**HEADERS, "Idempotency-Key": "parent"}
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    _checkpoint(db_conn, parent_id, "original-checkpoint")
    http = MagicMock()
    lose = True

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        nonlocal lose
        response = client.post(path, **kwargs)
        if lose:
            lose = False
            assert response.status_code == 201, response.text
            raise httpx.ReadTimeout("committed fork response lost")
        return _response(response)

    http.post.side_effect = submit
    with transport.use_client(http):
        with pytest.raises(GatewayUnavailable):
            _sdk(fork_from=parent_id, idempotency_key="fork-intent")
        assert http.post.call_count == 1
        row = db_conn.execute(
            "SELECT id, fork_source_agent_id, fork_source_checkpoint_id, last_launch_attempt_id "
            "FROM agents_meta WHERE fork_source_agent_id=%s",
            (parent_id,),
        ).fetchone()
        assert row is not None
        child_id, source_id, checkpoint_id, attempt_id = row
        assert (source_id, checkpoint_id) == (parent_id, "original-checkpoint")
        if source_change == "advanced":
            _checkpoint(db_conn, parent_id, "later-checkpoint")
        else:
            db_conn.execute("DELETE FROM checkpoints WHERE thread_id=%s", (str(parent_id),))
            db_conn.commit()

        def unexpected(*args: object, **kwargs: object) -> None:
            raise AssertionError("fork replay must precede mutable source preflight")

        monkeypatch.setattr(agent_router, "spawn_prechecks_blocking", unexpected)
        assert _sdk(fork_from=parent_id, idempotency_key="fork-intent") == child_id

    assert http.post.call_count == 2
    assert http.post.call_args_list[0] == http.post.call_args_list[1]
    assert db_conn.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s", (str(child_id),)
    ).fetchall() == [("original-checkpoint",)]
    assert db_conn.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id=%s ORDER BY id", (child_id,)
    ).fetchall() == [("fork",), ("chat",)]
    assert db_conn.execute(
        "SELECT launch_attempt_id FROM agent_creation_snapshots WHERE agent_id=%s", (child_id,)
    ).fetchone() == (attempt_id,)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (2,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='fork'"
    ).fetchone() == (1,)
