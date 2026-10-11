"""Agent-to-agent events: who did what to whom is read from `source` and `agent_id`, never `target_agent_id`."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import LiteralString, cast

import psycopg
import pytest
from fastapi import HTTPException, Request

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.derived.insights.run_timeline import links
from tests.fixtures.units import spawn_agent

_INSERT: LiteralString = (
    "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
    "agent_id, target_agent_id, attributes) VALUES "
    "(%s, now() - (%s * interval '1 hour'), 'm', 'p', %s, 'info', %s, %s, %s, %s::jsonb)"
)


def record(
    conn: psycopg.Connection,
    name: str,
    *,
    source: str,
    agent: int,
    target: int | None = None,
    hours_ago: float = 1,
    attributes: dict[str, object] | None = None,
) -> None:
    conn.execute(
        _INSERT,
        (
            uuid.uuid4().int % (1 << 62),
            hours_ago,
            name,
            source,
            agent,
            target,
            json.dumps(attributes or {}),
        ),
    )
    conn.commit()


def message(
    conn: psycopg.Connection,
    *,
    receiver: int,
    sender: int,
    kind: str = "chat",
    hours_ago: float = 1,
    content: str = "hi",
) -> int:
    """A message the way delivery writes it: the inbound row, and the audit event naming it."""
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (receiver, content, kind, f"agent:{sender}"),
    ).fetchone()
    assert row is not None
    inbound_id = int(row[0])
    record(
        conn,
        "send_message",
        source=f"agent:{sender}",
        agent=receiver,
        target=sender,
        hours_ago=hours_ago,
        attributes={"inbound_id": inbound_id, "content": content},
    )
    return inbound_id


@pytest.fixture
def receiver(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority, *, database_gate: ProcessDbGate
) -> int:
    return spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )


def read(
    *agents: int, kinds: tuple[str, ...] | None = None, database_gate: ProcessDbGate
) -> list[links.RunTimelineLink]:
    """The links of the agents; `kinds` leaves out the user's spawn that creating a test agent records."""
    now = datetime.now(UTC)
    found = links.read(
        Database.from_settings(gate=database_gate), list(agents), now - timedelta(hours=48), now
    )
    return [link for link in found if kinds is None or link.kind in kinds]


def test_a_chat_message_goes_from_its_source_to_the_agent_it_was_written_to(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    # agent_id is the recipient, target_agent_id repeats the sender.
    inbound_id = message(db_conn, receiver=receiver, sender=405, content="do   the\nthing")
    [link] = read(receiver, kinds=("send_message",), database_gate=database_gate)
    assert (link.kind, link.sender, link.receiver) == ("send_message", 405, receiver)
    assert link.inbound_id == inbound_id
    assert not hasattr(link, "preview")  # the text is served on demand, not in the list


def test_only_chat_messages_are_links_a_task_assignment_is_not(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    chat = message(db_conn, receiver=receiver, sender=405, hours_ago=3)
    message(db_conn, receiver=receiver, sender=405, kind="system_note", hours_ago=2)
    # An audit row naming no inbound row, or one that does not exist, is not a chat either.
    record(db_conn, "send_message", source="agent:405", agent=receiver, target=405, hours_ago=1)
    record(
        db_conn,
        "send_message",
        source="agent:405",
        agent=receiver,
        target=405,
        attributes={"inbound_id": 999_999_999},
    )
    assert [
        link.inbound_id
        for link in read(receiver, kinds=("send_message",), database_gate=database_gate)
    ] == [chat]


def test_every_kind_reads_the_sender_from_source(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    record(db_conn, "spawn", source="agent:405", agent=6657, target=405, hours_ago=6)
    # A fork is executed by `source`; target_agent_id is the agent it was copied from.
    record(db_conn, "fork", source="agent:405", agent=6658, target=9, hours_ago=5)
    record(
        db_conn,
        "terminate",
        source="agent:405",
        agent=6657,
        hours_ago=4,
        attributes={"inbound_id": 55},
    )
    record(db_conn, "restart", source="agent:6657", agent=6658, hours_ago=3)
    record(db_conn, "resurrect", source="agent:405", agent=6657, target=405, hours_ago=2)
    seen = [
        (e.kind, e.sender, e.receiver, e.fork_from, e.inbound_id)
        for e in read(6657, 6658, database_gate=database_gate)
    ]
    assert seen == [
        ("spawn", 405, 6657, None, None),
        ("fork", 405, 6658, 9, None),
        ("terminate", 405, 6657, None, 55),
        ("restart", 6657, 6658, None, None),
        ("resurrect", 405, 6657, None, None),
    ]


def test_an_event_with_one_end_in_the_asked_agents_is_returned(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    message(db_conn, receiver=receiver, sender=405)
    assert [(e.sender, e.receiver) for e in read(405, database_gate=database_gate)] == [
        (405, receiver)
    ]
    assert [
        (e.sender, e.receiver)
        for e in read(receiver, kinds=("send_message",), database_gate=database_gate)
    ] == [(405, receiver)]
    assert read(406, kinds=("send_message",), database_gate=database_gate) == []


def test_events_that_are_not_between_agents_or_with_the_user_are_left_out(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    record(db_conn, "resurrect", source="system", agent=6657)
    record(db_conn, "restart", source="schedule:3", agent=6657)
    record(db_conn, "send_message", source="agent:6657", agent=6657, target=6657)  # to itself
    record(db_conn, "cancel", source="agent:405", agent=6657)  # not a link kind
    assert read(6657, 405, database_gate=database_gate) == []


def test_an_event_the_user_did_to_an_agent_has_no_sender(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    record(db_conn, "spawn", source="user", agent=6657, hours_ago=3)
    record(db_conn, "terminate", source="ui:page:agents", agent=6657, hours_ago=2)
    assert [(e.kind, e.sender, e.receiver) for e in read(6657, database_gate=database_gate)] == [
        ("spawn", None, 6657),
        ("terminate", None, 6657),
    ]


def test_a_notice_is_an_agent_posting_to_the_user(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    db_conn.execute(
        "INSERT INTO agent_notices (local_id, agent_id, title, priority, require_response, expire_at) "
        "VALUES (1, %s, 'need   a decision', 'P1', false, now() + interval '1 day')",
        (receiver,),
    )
    db_conn.commit()
    [link] = read(receiver, kinds=("notice",), database_gate=database_gate)
    assert (link.kind, link.sender, link.receiver) == ("notice", receiver, None)
    assert link.notice_id is not None
    assert read(receiver + 1, kinds=("notice",), database_gate=database_gate) == []


def test_an_unknown_source_prefix_fails_instead_of_being_skipped(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    record(db_conn, "terminate", source="bogus:5", agent=6657)
    with pytest.raises(ValueError, match="Unrecognized inbound source"):
        read(6657, database_gate=database_gate)


def test_a_window_longer_than_a_page_is_read_completely(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    receiver: int,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(links, "_PAGE_SIZE", 2)
    for hours in (5, 4, 3, 2, 1):
        message(db_conn, receiver=receiver, sender=405, hours_ago=hours)
    assert len(read(receiver, kinds=("send_message",), database_gate=database_gate)) == 5


def _content(*, database_gate: ProcessDbGate, **params: int):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=Database.from_settings(gate=database_gate)))
    )
    return links.get_run_timeline_link_content(cast(Request, request), **params)


def test_the_full_text_of_a_message_is_served_uncut(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    long = "line one\n\n" + "x" * 5000
    inbound_id = message(db_conn, receiver=receiver, sender=405, content=long)
    served = _content(inbound_id=inbound_id, database_gate=database_gate)
    assert (served.title, served.content) == (None, long)


def test_only_a_chat_message_is_served(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    note = message(db_conn, receiver=receiver, sender=405, kind="system_note")
    with pytest.raises(HTTPException) as gone:
        _content(inbound_id=note, database_gate=database_gate)
    assert gone.value.status_code == 404


def test_a_notice_is_served_with_title_and_text(
    db_conn: psycopg.Connection, receiver: int, *, database_gate: ProcessDbGate
) -> None:
    row = db_conn.execute(
        "INSERT INTO agent_notices (local_id, agent_id, title, content, priority, require_response, expire_at) "
        "VALUES (1, %s, 'a title', '## body', 'P1', false, now() + interval '1 day') RETURNING id",
        (receiver,),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    served = _content(notice_id=int(row[0]), database_gate=database_gate)
    assert (served.title, served.content) == ("a title", "## body")


def test_exactly_one_reference_is_required(*, database_gate: ProcessDbGate) -> None:
    for params in ({}, {"inbound_id": 1, "notice_id": 1}):
        with pytest.raises(HTTPException) as bad:
            _content(database_gate=database_gate, **params)
        assert bad.value.status_code == 422
