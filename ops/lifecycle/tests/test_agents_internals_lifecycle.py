from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest

from base.agents.messages.envelope import EnvelopeReadInputs, wrap_inbound
from base.api_contracts.agent_contract import AgentNotFound, ResurrectAlreadyAlive, ResurrectError
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="user",
            prompt="resume work",
            local_machine=machine_identity.name(),
        )

        assert returned == agent_id
        row = agents_row(db_conn, agent_id)
        assert row is not None
        assert row[2] == "idling"
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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="user",
            local_machine=machine_identity.name(),
        )

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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="agent:42",
            prompt="resume work",
            local_machine=machine_identity.name(),
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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="user",
            prompt="catch up with #341",
            local_machine=machine_identity.name(),
        )

        rows = inbound_rows(db_conn, agent_id)
        assert rows == [
            ("", "resurrect", "user"),
            ("catch up with #341", "chat", "user"),
        ]
        for content, kind, source in rows:
            if kind == "chat":
                assert source is not None
                wrap_inbound(content, source, inputs=_ENVELOPE_INPUTS)

    def test_resurrect_nonexistent_raises_agent_not_found(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(AgentNotFound, match="does not exist"):
            resurrect_agent(
                database,
                event_bus,
                9999,
                resurrected_by="user",
                prompt="test",
                local_machine=machine_identity.name(),
            )

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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        if alive_status != "idling":
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = %s WHERE id = %s",
                    (alive_status, agent_id),
                )
            db_conn.commit()
        with pytest.raises(ResurrectAlreadyAlive, match=alive_status):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="user",
                prompt="test",
                local_machine=machine_identity.name(),
            )
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
        machine_identity: MachineIdentity,
    ) -> None:
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)
        original_fetchone = psycopg.Cursor.fetchone
        status_select_cursors: set[int] = set()

        def tracking_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            result = original_execute(self, query, *args, **kwargs)
            if query == wake.RESURRECTION_ROW:
                status_select_cursors.add(id(self))
            return result

        def lying_fetchone(self: Any) -> Any:
            if id(self) in status_select_cursors:
                status_select_cursors.remove(id(self))
                row = original_fetchone(self)
                assert row is not None
                return ("terminated", *row[1:])
            return original_fetchone(self)

        monkeypatch.setattr(psycopg.Cursor, "execute", tracking_execute)
        monkeypatch.setattr(psycopg.Cursor, "fetchone", lying_fetchone)

        with pytest.raises(ResurrectAlreadyAlive, match="concurrently modified"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="user",
                prompt="test",
                local_machine=machine_identity.name(),
            )

        monkeypatch.undo()
        assert inbound_count(db_conn, agent_id) == 0
        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"

    def test_subclasses_inherit_from_resurrect_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ResurrectError):
            resurrect_agent(
                database,
                event_bus,
                9999,
                resurrected_by="user",
                prompt="test",
                local_machine=machine_identity.name(),
            )
