"""Resurrecting an agent: row, inbound, hosted force-settle and wake behavior of `ops.lifecycle`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import base.db
from base.agents import AgentNotFound, ResurrectAlreadyAlive, ResurrectError
from base.agents.incarnation.resources import IncarnationResources
from base.agents.messages.envelope import EnvelopeReadInputs, wrap_inbound
from base.agents.messages.inbound import InboundKind
from base.clock import Clock
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import create_agent_row, resurrect_agent, wake
from ops.agents.wake import ResurrectTriggerStaleError
from ops.lifecycle import force_mark_terminated
from ops.tests.pool_support import make_test_pool

_ENVELOPE_INPUTS = EnvelopeReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def _noop(_db: object, _bus: object, *_args: object, **_kwargs: object) -> None:
    return None


def _agents_row(db: psycopg.Connection, agent_id: int) -> tuple[int, str, str, int | None] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, status, pid FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def _inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _spawn_agent(
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> int:
    """Test setup helper — mirrors the pre-#1236 `spawn_agent()` contract
    (create row + launch) as the two-phase split: `create_agent_row`
    (gateway-side, the main data-plane identity) then `_launch_agent_process`
    (runner-side), with the launch stubbed by the autouse guard. The launch op's
    prompt-delivery half is covered in ops/lifecycle/tests/test_operations.py."""
    agent_id, _birth_config, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(),
        EventBus.from_settings(),
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=machine_name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
        catalog=model_catalog,
        authority=config_authority,
    )
    base.db.publish_inbound_wake(Database.from_settings(), EventBus.from_settings(), agent_id, "0")
    return agent_id


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str | None]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


def _hosted_agent(
    db: psycopg.Connection, *, config_authority: ConfigAuthority, model_catalog: ModelCatalog
) -> int:
    """Seed the retained authority of a hosted incarnation for guard tests."""
    agent_id = _spawn_agent(config_authority=config_authority, model_catalog=model_catalog)
    generation, owner = uuid4(), uuid4()
    resources = IncarnationResources(generation=generation, owner=owner, requests={})
    db.execute(
        "UPDATE agents_meta SET runtime_kind='hosted', runtime_generation=%s, "
        "runtime_owner=%s, incarnation_resources=%s WHERE id=%s",
        (generation, owner, Jsonb(resources.model_dump(mode="json")), agent_id),
    )
    db.commit()
    return agent_id


async def _settle_hosted_force(
    db: psycopg.Connection, pool: AsyncConnectionPool, agent_id: int
) -> None:
    """Complete this inactive fixture through its retained hosted owner."""
    from base.agents.incarnation.hosted_force import original_host_force

    row = db.execute("SELECT runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert row is not None
    db.commit()
    assert await original_host_force(pool, agent_id, row[0], machine_name(), quiescent=True)


class TestResurrectAgent:
    async def test_repeat_force_fence_preserves_page_reopen_epoch(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A repeated force creates a newer intent fence without changing the
        real status-transition epoch used to reopen pages on manual resurrect."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pages (agent_id, name, port) VALUES (%s, 'work', 8765)",
                (agent_id,),
            )
        db_conn.commit()

        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status_changed_at, last_force_terminate_inbound_id "
                    "FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                first_agent_row = cur.fetchone()
                cur.execute(
                    "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                    (agent_id,),
                )
                first_page_row = cur.fetchone()
            assert first_agent_row is not None and first_page_row is not None
            assert first_page_row[0] == first_agent_row[0]

            force_mark_terminated(database, event_bus, agent_id, pool)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status_changed_at, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            repeated_agent_row = cur.fetchone()
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            repeated_page_row = cur.fetchone()
        assert repeated_agent_row is not None and repeated_page_row is not None
        assert repeated_agent_row[0] == first_agent_row[0]
        assert repeated_agent_row[1] > first_agent_row[1]
        assert repeated_page_row[0] == first_page_row[0]

        await _settle_hosted_force(db_conn, aops_pool, agent_id)

        resurrect_agent(database, event_bus, agent_id, resurrected_by="user")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            reopened_page_row = cur.fetchone()
        assert reopened_page_row == (None,)

    async def test_guarded_resurrect_rejects_chat_below_latest_force_fence(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """Even without a real status transition, a repeated explicit force
        fences every chat inbound that existed before that latest intent."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "work before repeated force",
            source="user",
            bus=event_bus,
            database=database,
        )
        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)

        await _settle_hosted_force(db_conn, aops_pool, agent_id)

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_guarded_resurrect_rejects_chat_from_prior_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A watchdog task selected for one death must not revive a later
        explicit kill while its RPC was in flight."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "wake after first death",
            source="user",
            bus=event_bus,
            database=database,
        )

        # The agent came back by another path and was explicitly killed again
        # before the watchdog's original RPC reached its home runner.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 - interval '1 second' "
                "WHERE id = %s",
                (agent_id, trigger_id),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert _inbound_rows(db_conn, agent_id) == [("wake after first death", "chat", "user")]

    def test_guarded_resurrect_rejects_chat_that_is_no_longer_pending(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A trigger claimed while the RPC is in flight no longer justifies
        launching the terminated owner."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn, agent_id, "already handled", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (trigger_id,),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert _inbound_rows(db_conn, agent_id) == [("already handled", "chat", "user")]

    def test_guarded_resurrect_accepts_pending_chat_after_current_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A pending chat created after the current death still auto-wakes the
        agent, preserving the post-termination delivery contract."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "new work after death",
            source="user",
            bus=event_bus,
            database=database,
        )
        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=trigger_id,
            trigger_inbound_kind=InboundKind.CHAT,
        )

        assert returned == agent_id
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"
        assert _inbound_rows(db_conn, agent_id) == [
            ("new work after death", "chat", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_resurrect_accepts_exact_pending_compact_after_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """UI compact is guarded work too: its exact durable id and expected
        kind qualify only while pending after the current death."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )
        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=compact_id,
            trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
        )

        assert returned == agent_id
        assert _agents_row(db_conn, agent_id)[2] == "idling"  # type: ignore[index]
        assert _inbound_rows(db_conn, agent_id) == [
            ("", "compact_request", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_compact_rejects_kind_mismatch_and_claimed_trigger(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """The caller's expected kind is part of the CAS, and a compact that
        has already been claimed no longer licenses a new process."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (compact_id,),
            )
        db_conn.commit()
        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
            )

        assert _agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]

    async def test_guarded_compact_below_force_fence_is_stale(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A force after compact enqueue fences that older work exactly like
        chat, even though no second status transition occurs."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )
        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)

        await _settle_hosted_force(db_conn, aops_pool, agent_id)

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
            )
        assert _agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]

    def test_resurrects_terminated_agent_and_inserts_resurrect_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """Terminated -> unclaimed idling with durable resurrection and optional chat."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        # simulate terminate path: UPDATE 'idling' → 'terminated' (retaining the hosted incarnation)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(
            database, event_bus, agent_id, resurrected_by="user", prompt="resume work"
        )

        assert returned == agent_id
        row = _agents_row(db_conn, agent_id)
        assert row is not None
        assert row[2] == "idling"  # unclaimed, waiting for the host to admit and UPDATE 'running'
        # resurrect inserts lifecycle inbound — content empty, trigger written to source field;
        # prompt as chat inbound follows in the same transaction
        rows = _inbound_rows(db_conn, agent_id)
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
    ) -> None:
        """The UI resurrect button is a pure lifecycle event — no prompt. Only
        the kind='resurrect' marker inbound is written; no chat inbound. The
        agent still wakes (the marker is the "ok I'm awake" signal)."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(database, event_bus, agent_id, resurrected_by="user")

        assert returned == agent_id
        assert _inbound_rows(db_conn, agent_id) == [("", "resurrect", "user")]

    def test_resurrect_records_resurrected_by_in_inbound_source(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """resurrected_by is written as-is into the inbound source field (not into content), so that claim
        can compose it into the lifecycle marker during dispatch. SDK paths pass 'agent:N', gateway passes 'user'."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database, event_bus, agent_id, resurrected_by="agent:42", prompt="resume work"
        )

        rows = _inbound_rows(db_conn, agent_id)
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
    ) -> None:
        """A resurrect with a prompt writes two inbounds (lifecycle + chat); the chat inbound reuses
        resurrected_by as its source — that value must survive envelope wrap, otherwise
        the successor turn fails with a ValueError on its first claim (agent-240 incident)."""
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(
            database, event_bus, agent_id, resurrected_by="user", prompt="catch up with #341"
        )

        rows = _inbound_rows(db_conn, agent_id)
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
    ) -> None:
        """Any status other than 'terminated' cannot be resurrected — only 'terminated' is a valid source state.

        Full parametrization locks the contract that "resurrect refuses all states that are still alive or not fully dead".
        Historically only running/idling were tested. The complete current
        non-terminal set is covered explicitly — a regression that changed the guard
        to `if current in [...]` and missed a value would silently let resurrect
        send a revival notification to an agent that is "still running / still init'ing",
        with the production consequence of a dual-incarnation race on the same agent_id.
        """
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
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
        assert _inbound_count(db_conn, agent_id) == 0

    def test_resurrect_select_update_race_does_not_insert_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """SELECT sees 'terminated' → after SELECT the row is concurrently changed to 'idling' → UPDATE
        WHERE status='terminated' hits 0 rows — at this point we must **not** proceed to INSERT a fake revival
        notification for a already-live agent (that would send an hallucination signal to the running process).

        Simulate: make the first fetchone falsely report 'terminated' while the underlying row is actually 'idling'
        — equivalent to "status was rewritten after SELECT". The code must raise when UPDATE rowcount=0.
        """
        agent_id = _hosted_agent(
            db_conn, config_authority=config_authority, model_catalog=model_catalog
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
        assert _inbound_count(db_conn, agent_id) == 0
        # key invariant 2: status unchanged (UPDATE 0 rows + raise rolls back entire transaction)
        row = _agents_row(db_conn, agent_id)
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
