"""Agent-to-agent events: who did what to whom is read from `source` and `agent_id`, never `target_agent_id`."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import LiteralString

import psycopg
import pytest

from base.config.service_read import ConfigAuthority
from base.db import Database
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
def receiver(model_catalog: ModelCatalog, config_authority: ConfigAuthority) -> int:
    return spawn_agent(catalog=model_catalog, authority=config_authority)


def read(*agents: int) -> list[links.RunTimelineLink]:
    now = datetime.now(UTC)
    return links.read(Database.from_settings(), list(agents), now - timedelta(hours=48), now)


def test_a_chat_message_goes_from_its_source_to_the_agent_it_was_written_to(
    db_conn: psycopg.Connection, receiver: int
) -> None:
    # agent_id is the recipient, target_agent_id repeats the sender.
    inbound_id = message(db_conn, receiver=receiver, sender=405, content="do   the\nthing")
    [link] = read(receiver)
    assert (link.kind, link.sender, link.receiver) == ("send_message", 405, receiver)
    assert (link.inbound_id, link.preview) == (inbound_id, "do the thing")


def test_only_chat_messages_are_links_a_task_assignment_is_not(
    db_conn: psycopg.Connection, receiver: int
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
    assert [link.inbound_id for link in read(receiver)] == [chat]


def test_every_kind_reads_the_sender_from_source(db_conn: psycopg.Connection) -> None:
    record(db_conn, "spawn", source="agent:405", agent=6657, target=405, hours_ago=6)
    # A fork is executed by `source`; target_agent_id is the agent it was copied from.
    record(db_conn, "fork", source="agent:405", agent=6658, target=9, hours_ago=5)
    record(db_conn, "terminate", source="agent:405", agent=6657, hours_ago=4)
    record(db_conn, "restart", source="agent:6657", agent=6658, hours_ago=3)
    record(db_conn, "resurrect", source="agent:405", agent=6657, target=405, hours_ago=2)
    seen = [(e.kind, e.sender, e.receiver, e.fork_from) for e in read(6657, 6658)]
    assert seen == [
        ("spawn", 405, 6657, None),
        ("fork", 405, 6658, 9),
        ("terminate", 405, 6657, None),
        ("restart", 6657, 6658, None),
        ("resurrect", 405, 6657, None),
    ]


def test_an_event_with_one_end_in_the_asked_agents_is_returned(
    db_conn: psycopg.Connection, receiver: int
) -> None:
    message(db_conn, receiver=receiver, sender=405)
    assert [(e.sender, e.receiver) for e in read(405)] == [(405, receiver)]
    assert [(e.sender, e.receiver) for e in read(receiver)] == [(405, receiver)]
    assert read(406) == []


def test_events_that_are_not_between_two_agents_are_left_out(db_conn: psycopg.Connection) -> None:
    record(db_conn, "spawn", source="user", agent=6657)
    record(db_conn, "resurrect", source="system", agent=6657)
    record(db_conn, "send_message", source="agent:6657", agent=6657, target=6657)  # to itself
    record(db_conn, "cancel", source="agent:405", agent=6657)  # not an agent-to-agent kind
    assert read(6657, 405) == []


def test_an_unknown_source_prefix_fails_instead_of_being_skipped(
    db_conn: psycopg.Connection,
) -> None:
    record(db_conn, "terminate", source="bogus:5", agent=6657)
    with pytest.raises(ValueError, match="Unrecognized inbound source"):
        read(6657)


def test_a_window_longer_than_a_page_is_read_completely(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, receiver: int
) -> None:
    monkeypatch.setattr(links, "_PAGE_SIZE", 2)
    for hours in (5, 4, 3, 2, 1):
        message(db_conn, receiver=receiver, sender=405, hours_ago=hours)
    assert len(read(receiver)) == 5
