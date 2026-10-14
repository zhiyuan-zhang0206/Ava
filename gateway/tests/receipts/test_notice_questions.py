"""Structured questions commit one numbered answer and one recoverable inbound."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.events.test_notices_endpoint import seed_agent
from gateway.tests.receipts.test_current_notice_receipts import client as client

QUESTION = {
    "type": "multi_select",
    "options": [
        {"id": "web", "label": "Web", "description": None},
        {"id": "desktop", "label": "Desktop", "description": None},
    ],
    "allow_other": True,
}


def post_question(client: TestClient, agent: int, question: dict[str, object] | None = None) -> int:
    response = client.post(
        f"/api/agents/{agent}/notices",
        json={
            "title": "Which surfaces?",
            "content": "Choose the scope.",
            "require_response": True,
            "question": QUESTION if question is None else question,
        },
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    feed = client.get("/api/notices").json()
    notice = next(n for n in feed["awaiting"] if n["agent_id"] == agent)
    assert notice["question"] == (QUESTION if question is None else question)
    return notice["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "answer", "answer": {"option_ids": ["desktop"], "other_text": "Use 1 later."}},
        {"action": "answer", "reply": "2,3\nUse 1 later."},
    ],
)
def test_answer_round_trip_and_retry_deliver_one_numbered_inbound(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"
    headers = keyed_headers(str(uuid4()))
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert client.post(path, json=body, headers=headers).json() == first.json()
    row = db_conn.execute(
        "SELECT resolution,answer,reply FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone()
    assert row is not None
    assert row[0] == "answered"
    assert row[1]["option_ids"] == ["desktop"]
    assert row[1]["other_text"] == "Use 1 later."
    assert "1. Web\n2. Desktop\n3. Other" in row[2]
    assert "Selected: 2, 3" in row[2]
    inbound = db_conn.execute(
        "SELECT source,content FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchall()
    assert len(inbound) == 1
    assert inbound[0][0] == "system:notice-reply"
    assert "Other (3): Use 1 later." in inbound[0][1]
    resolved = client.get("/api/notices").json()["resolved_page"]
    assert next(n for n in resolved if n["id"] == notice)["answer"] == row[1]


@pytest.mark.parametrize(
    "answer", [{}, {"option_ids": ["missing"]}, {"text": "yes"}, {"other_text": " "}]
)
def test_invalid_answer_has_no_resolution_or_inbound(
    client: TestClient, db_conn: psycopg.Connection, answer: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve",
        json={"action": "answer", "answer": answer},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT resolved_at,answer FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None, None)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_question_context_is_frozen_but_priority_can_change(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    path = f"/api/agents/{agent}/notices/current/guarded-v1"
    for changes in (
        {"title": "Changed?"},
        {"content": "Different meaning"},
        {"question": {"type": "text"}},
    ):
        response = client.patch(
            path,
            json={"observed_notice_id": notice, **changes},
            headers=keyed_headers(str(uuid4())),
        )
        assert response.status_code == 422, response.text
    assert (
        client.patch(
            path,
            json={"observed_notice_id": notice, "priority": "P1"},
            headers=keyed_headers(str(uuid4())),
        ).status_code
        == 200
    )


def test_open_question_and_dismissal_have_distinct_effects(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent, {"type": "text"})
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve",
        json={"action": "answer", "answer": {"text": "Keep it readable."}},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    later = post_question(client, agent)
    dismissed = client.post(
        f"/api/keyed/v1/agents/{agent}/notices/{later}/resolve",
        json={"action": "dismiss"},
        headers=keyed_headers(str(uuid4())),
    )
    assert dismissed.status_code == 201
    assert db_conn.execute(
        "SELECT resolution,answer,reply FROM agent_notices WHERE id=%s", (later,)
    ).fetchone() == ("dismissed", None, None)


def test_expired_question_cannot_receive_a_late_answer(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    db_conn.execute(
        "UPDATE agent_notices SET expire_at=now()-interval '1 second' WHERE id=%s", (notice,)
    )
    db_conn.commit()
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve",
        json={"action": "answer", "answer": {"option_ids": ["web"]}},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 409
    assert db_conn.execute(
        "SELECT answer FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None,)


def test_invalid_question_does_not_supersede_the_current_question(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    for question in ({"type": "unknown"}, {"type": "single_select", "options": []}):
        response = client.post(
            f"/api/agents/{agent}/notices",
            json={"title": "Invalid", "require_response": True, "question": question},
            headers=keyed_headers(str(uuid4())),
        )
        assert response.status_code == 422
    response = client.post(
        f"/api/agents/{agent}/notices",
        json={"title": "FYI with a question", "question": QUESTION},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 422
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None,)


def test_inspector_carries_the_same_question(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    post_question(client, agent)
    response = client.get(f"/api/agents/{agent}/inspect/live")
    assert response.status_code == 200, response.text
    assert response.json()["notice"]["question"] == QUESTION
