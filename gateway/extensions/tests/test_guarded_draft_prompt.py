"""A committed raw package intent survives a changed server-owned prompt frame."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.agents.tests.test_guarded_creation import HEADERS
from gateway.agents.tests.test_guarded_creation import client as client
from gateway.extensions import packages
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process


def test_original_package_prompt_survives_template_change(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/api/keyed/v1/packages/draft"
    body = {"kind": "skill", "nl": "one intent"}
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 200, first.text
    original = db_conn.execute("SELECT content FROM inbound_messages").fetchone()
    monkeypatch.setitem(packages._KIND_BRIEF, "skill", "a changed template")
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert db_conn.execute("SELECT content FROM inbound_messages").fetchall() == [original]
