"""Resurrecting an agent: row, inbound, hosted force-settle and wake behavior of `ops.lifecycle`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest

from base.agents import AgentNotFound, ResurrectAlreadyAlive, ResurrectError
from base.agents.messages.envelope import EnvelopeReadInputs, wrap_inbound
from base.clock import Clock
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import resurrect_agent, wake
from ops.lifecycle.tests.resurrection_setup import (
    agents_row,
    hosted_agent,
    inbound_count,
    inbound_rows,
)

_ENVELOPE_INPUTS = EnvelopeReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


class TestResurrectAgent:
    def test_resurrects_terminated_agent_and_inserts_resurrect_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Terminated -> unclaimed idling with durable resurrection and optional chat."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        # simulate terminate path: UPDATE 'idling' → 'terminated' (retaining the hosted incarnation)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(
            database, event_bus, agent_id, resurrected_by="user", prompt="resume work"
        )

        assert returned == agent_id
        row = agents_row(db_conn, agent_id)
        assert row is not None
        assert row[2] == "idling"  # unclaimed, waiting for the host to admit and UPDATE 'running'
        # resurrect inserts lifecycle inbound — content empty, trigger written to source field;
        # prompt as chat inbound follows in the same transaction
        rows = inbound_rows(db_conn, agent_id)
        assert rows == [("", "resurrect", "user"), ("resume work", "chat", "user")]

    def test_resurrect_without_prompt_inserts_only_lifecycle_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """The UI resurrect button is a pure lifecycle event — no prompt. Only
        the kind='resurrect' marker inbound is written; no chat inbound. The
        agent still wakes (the marker is the "ok I'm awake" signal)."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(database, event_bus, agent_id, resurrected_by="user")

        assert returned == agent_id
        assert inbound_rows(db_conn, agent_id) == [("", "resurrect", "user")]

    def test_resurrect_records_resurrected_by_in_inbound_source(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """resurrected_by is written as-is into the inbound source field (not into content), so that claim
        can compose it into the lifecycle marker during dispatch. SDK paths pass 'agent:N', gateway passes 'user'."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database, event_bus, agent_id, resurrected_by="agent:42", prompt="resume work"
        )

        rows = inbound_rows(db_conn, agent_id)
        assert rows == [
            ("", "resurrect", "agent:42"),
            ("resume work", "chat", "agent:42"),
        ]

    def test_resurrect_with_prompt_chat_inbound_has_wrappable_source(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A resurrect with a prompt writes two inbounds (lifecycle + chat); the chat inbound reuses
        resurrected_by as its source — that value must survive envelope wrap, otherwise
        the successor turn fails with a ValueError on its first claim (agent-240 incident)."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database, event_bus, agent_id, resurrected_by="user", prompt="catch up with #341"
        )

        rows = inbound_rows(db_conn, agent_id)
        assert rows == [
            ("", "resurrect", "user"),
            ("catch up with #341", "chat", "user"),
        ]
        # The chat inbound's source must be a valid value accepted by the claim-side wrap
        for content, kind, source in rows:
            if kind == "chat":
                assert source is not None
                wrap_inbound(
                    content, source, inputs=_ENVELOPE_INPUTS
                )  # raises ValueError on illegal source

    def test_resurrect_nonexistent_raises_agent_not_found(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        with pytest.raises(AgentNotFound, match="does not exist"):
            resurrect_agent(database, event_bus, 9999, resurrected_by="user", prompt="test")

    @pytest.mark.parametrize(
        "alive_status",
        ["running", "idling"],
    )
    def test_resurrect_alive_agent_raises_already_alive(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        alive_status: str,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Any status other than 'terminated' cannot be resurrected — only 'terminated' is a valid source state.

        Full parametrization locks the contract that "resurrect refuses all states that are still alive or not fully dead".
        Historically only running/idling were tested. The complete current
        non-terminal set is covered explicitly — a regression that changed the guard
        to `if current in [...]` and missed a value would silently let resurrect
        send a revival notification to an agent that is "still running / still init'ing",
        with the production consequence of a dual-incarnation race on the same agent_id.
        """
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        # the helper leaves 'idling'; other statuses are explicitly set via UPDATE for the test
        if alive_status != "idling":
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = %s WHERE id = %s",
                    (alive_status, agent_id),
                )
            db_conn.commit()
        with pytest.raises(ResurrectAlreadyAlive, match=alive_status):
            resurrect_agent(database, event_bus, agent_id, resurrected_by="user", prompt="test")
        # The failure path must not insert inbound (transaction inner raise prevents commit)
        assert inbound_count(db_conn, agent_id) == 0

    def test_resurrect_select_update_race_does_not_insert_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """SELECT sees 'terminated' → after SELECT the row is concurrently changed to 'idling' → UPDATE
        WHERE status='terminated' hits 0 rows — at this point we must **not** proceed to INSERT a fake revival
        notification for a already-live agent (that would send an hallucination signal to the running process).

        Simulate: make the first fetchone falsely report 'terminated' while the underlying row is actually 'idling'
        — equivalent to "status was rewritten after SELECT". The code must raise when UPDATE rowcount=0.
        """
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )  # real status='idling'

        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)
        original_fetchone = psycopg.Cursor.fetchone
        status_select_cursors: set[int] = set()

        def tracking_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            result = original_execute(self, query, *args, **kwargs)
            if query == wake._RESURRECTION_ROW:
                status_select_cursors.add(id(self))
            return result

        def lying_fetchone(self: Any) -> Any:
            if id(self) in status_select_cursors:
                status_select_cursors.remove(id(self))
                # enter UPDATE while preserving placement
                row = original_fetchone(self)
                assert row is not None
                return ("terminated", *row[1:])
            return original_fetchone(self)

        monkeypatch.setattr(psycopg.Cursor, "execute", tracking_execute)
        monkeypatch.setattr(psycopg.Cursor, "fetchone", lying_fetchone)

        with pytest.raises(ResurrectAlreadyAlive, match="concurrently modified"):
            resurrect_agent(database, event_bus, agent_id, resurrected_by="user", prompt="test")

        # key invariant 1: did not deliver a fake notification to a live agent
        monkeypatch.undo()  # restore fetchone so subsequent queries work
        assert inbound_count(db_conn, agent_id) == 0
        # key invariant 2: status unchanged (UPDATE 0 rows + raise rolls back entire transaction)
        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"

    def test_subclasses_inherit_from_resurrect_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Both subclasses belong to ResurrectError — a coarse catch with ResurrectError can catch them."""
        with pytest.raises(ResurrectError):
            resurrect_agent(database, event_bus, 9999, resurrected_by="user", prompt="test")
