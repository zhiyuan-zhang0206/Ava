"""SDK system notes retain agent attribution and task tags through actual Gateway routes."""

from uuid import uuid4

import psycopg
import pytest

import ava
from ava.agents import AgentNotFound
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from gateway.tests.agents.sdk_support import (
    sdk_via_gateway as sdk_via_gateway,
)
from gateway.tests.agents.sdk_support import spawn_agent
from tests.fixtures.pin_agent import pin_agent


class TestSendSystemNote:
    def test_send_system_note_inserts_system_note_inbound_with_task_tag(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """send_system_note posts a kind='system_note' inbound (agent source +
        task note tag) — never a peer chat row."""
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        peer_id = ava.agents.spawn(idempotency_key=str(uuid4()))

        inbound_id = ava.agents.send_system_note(
            peer_id,
            'Task #1 "t" is now assigned to you (by agent #1).',
            idempotency_key=str(uuid4()),
        )
        assert isinstance(inbound_id, int)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages WHERE agent_id = %s",
                (peer_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        content, kind, source, payload = rows[0]
        assert kind == "system_note"
        assert source == f"agent:{ava.self.AGENT_ID}"
        assert "assigned to you" in content
        assert payload == {"note_tag": "task", "delivery_resurrect": True}

    def test_send_system_note_preserves_explicit_task_id(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        peer_id = ava.agents.spawn(idempotency_key=str(uuid4()))
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, created_by, owner) "
                "VALUES ('task note target', 'd', 'user', %s) RETURNING id",
                (peer_id,),
            )
            row = cur.fetchone()
        assert row is not None
        task_id = row[0]
        db_conn.commit()

        ava.agents.send_system_note(
            peer_id,
            'Task #42 "t" is now assigned to you.',
            task_id=task_id,
            idempotency_key=str(uuid4()),
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT payload FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"note_tag": "task", "task_id": task_id, "delivery_resurrect": True}

    def test_send_system_note_to_terminated_is_fine(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A note with resurrect=True (task assignment) reaches a terminated
        agent — auto-resurrect is the gateway delivery detail, the SDK just
        posts the note and returns its id."""
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        peer_id = ava.agents.spawn(idempotency_key=str(uuid4()))
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer_id,))
        db_conn.commit()

        inbound_id = ava.agents.send_system_note(
            peer_id, 'Task #1 "t" is now assigned to you.', idempotency_key=str(uuid4())
        )
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            kinds = [row[0] for row in cur.fetchall()]
        assert "system_note" in kinds

    def test_send_system_note_normalizes_tuple_content(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """Same trailing-comma class as send_message — a one-element tuple
        unwraps to the note text instead of 422ing the gateway."""
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        peer_id = ava.agents.spawn(idempotency_key=str(uuid4()))

        # Runtime value of `("Task #1 is now " "assigned to you.",)`: implicit
        # concatenation plus trailing comma.
        content: object = ("Task #1 is now assigned to you.",)
        inbound_id = ava.agents.send_system_note(peer_id, content, idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Task #1 is now assigned to you."

    def test_send_system_note_rejects_multi_element_content(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A multi-element tuple is a coding mistake — TypeError, never joined."""
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        peer_id = ava.agents.spawn(idempotency_key=str(uuid4()))

        content: object = ("Task #1 is now ", "assigned to you.")
        with pytest.raises(TypeError, match="content must be a string"):
            ava.agents.send_system_note(peer_id, content, idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_send_system_note_to_nonexistent_raises(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        pin_agent(spawn_agent(config_authority=config_authority, database_gate=database_gate))
        with pytest.raises(AgentNotFound):
            ava.agents.send_system_note(9999, "ghost", idempotency_key=str(uuid4()))
