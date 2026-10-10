# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Operations cases: resurrect if terminated placement."""

from __future__ import annotations

import psycopg
import pytest

from base.agents import ResurrectResult
from base.agents.messages.inbound import InboundKind
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops import lifecycle
from ops.cluster import rpc as cluster_rpc
from ops.lifecycle.tests.test_operations import _db
from ops.lifecycle.tests.test_operations import (
    stub_pool as stub_pool,
)
from ops.rpc_schemas import ResurrectAgentRequest, ResurrectAgentResponse


class TestResurrectIfTerminatedPlacement:
    """`resurrect_if_terminated` must run the resurrect on the agent's home
    machine (`agents_meta.machine`): local in-process, remote via a 'lifecycle'
    op to that host's ops server. Launching locally for a remote-homed agent
    trips the boot placement gate and crash-loops (the agent-1513 incident);
    an unreachable home machine skips the resurrect — the inbound is already
    queued, so the next delivery or a manual resurrect picks it up."""

    @pytest.fixture(autouse=True)
    def _default_unsuppressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _db, _aid: False)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _db, _aid: False)
        monkeypatch.setattr(lifecycle, "_clear_wake_suppression", lambda _db, _aid: None)
        # The notice guard reads the trigger row from the DB; these tests pin
        # dispatch placement, not the guard — default it to "not a notice".
        monkeypatch.setattr(
            lifecycle, "_system_notice_source_of_trigger", lambda _db, _aid, _iid: None
        )

    @pytest.mark.asyncio
    async def test_active_suppression_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _db, _aid: True)

        def _no_machine_read(_db, _aid: int) -> str:
            raise AssertionError("suppressed auto-resurrect must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=88,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_tripped_recovery_breaker_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        """A tripped recovery breaker (consecutive permanent provider
        rejections) refuses the automatic resurrect before any home contact,
        exactly like an active wake suppression (task #3617)."""
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _db, _aid: True)

        def _no_machine_read(_db, _aid: int) -> str:
            raise AssertionError("halted auto-resurrect must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=88,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_local_home_resurrects_in_process(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        """Local-homed resurrect dispatches to ops server first; falls back
        to in-process when the ops server is unreachable."""
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        called: dict[str, object] = {}
        dispatch_called: list[dict[str, object]] = []

        async def _fake_resurrect_op(
            _db: object,
            _bus: object,
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            called["agent_id"] = agent_id
            called["resurrected_by"] = body.resurrected_by
            called["trigger_inbound_id"] = trigger_inbound_id
            called["trigger_inbound_kind"] = trigger_inbound_kind
            return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_resurrect_op)
        cleared: list[int] = []

        def _record_clear(_db: object, agent_id: int) -> None:
            cleared.append(agent_id)

        monkeypatch.setattr(
            lifecycle,
            "_clear_wake_suppression",
            _record_clear,
        )

        async def _fake_dispatch(*args: object, **kwargs: object) -> dict:
            dispatch_called.append(kwargs)
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("ops server not reachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=88,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.IDLING
        # Dispatch was attempted (HTTP-uniform path)
        assert len(dispatch_called) == 1
        assert dispatch_called[0]["target_machine"] == "home-a"
        assert dispatch_called[0]["payload"] == {
            "path": "/api/agents/5/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system", "prompt": None},
            "trigger_inbound_id": 88,
            "trigger_inbound_kind": "chat",
        }
        # Fallback: in-process resurrect happened
        assert called == {
            "agent_id": 5,
            "resurrected_by": "system",
            "trigger_inbound_id": 88,
            "trigger_inbound_kind": "chat",
        }
        assert cleared == [5]

    @pytest.mark.asyncio
    async def test_remote_home_forwards_lifecycle_op(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        captured: dict[str, object] = {}
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _no_local(
            _db: object, _bus: object, *_a: object, **_kw: object
        ) -> ResurrectAgentResponse:
            raise AssertionError("remote-homed resurrect must not launch locally")

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _no_local)

        async def _fake_dispatch(
            _db: object, target_machine: str, kind: str, payload: dict, **_kw: object
        ) -> dict:
            captured.update(target=target_machine, kind=kind, payload=payload)
            return {"status": "spawned"}

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            7,
            trigger_inbound_id=99,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.IDLING
        assert captured["target"] == "wsl"
        assert captured["kind"] == "lifecycle"
        assert captured["payload"] == {
            "path": "/api/agents/7/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system", "prompt": None},
            "trigger_inbound_id": 99,
            "trigger_inbound_kind": "chat",
        }

    @pytest.mark.asyncio
    async def test_remote_home_unreachable_skips(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        event_bus: EventBus,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from base.agents import AgentStatus
        from ops.cluster.rpc import ClusterOpUnreachable

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise ClusterOpUnreachable("ops server for machine='wsl' unreachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        with caplog.at_level("INFO"):
            status = await lifecycle.resurrect_if_terminated(
                _db(database_gate=database_gate),
                event_bus,
                7,
                trigger_inbound_id=99,
                trigger_inbound_kind=InboundKind.CHAT,
            )
        assert status is AgentStatus.TERMINATED
        assert "home machine unreachable" in caplog.text

    @pytest.mark.asyncio
    async def test_unknown_remote_op_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        from base.agents import AgentStatus
        from ops.cluster.rpc import ClusterOpFailed

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _failed(*_a: object, **_kw: object) -> dict:
            raise ClusterOpFailed({"error": "launch failed on the home machine"})

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)

        with pytest.raises(ClusterOpFailed):
            await lifecycle.resurrect_if_terminated(
                _db(database_gate=database_gate),
                event_bus,
                7,
                trigger_inbound_id=99,
                trigger_inbound_kind=InboundKind.CHAT,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("remote", [False, True])
    async def test_known_resurrection_refusal_keeps_inbound_queued(
        self,
        monkeypatch: pytest.MonkeyPatch,
        event_bus: EventBus,
        remote: bool,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from base.agents import AgentStatus, ResurrectRefused
        from ops.cluster.rpc import ClusterOpFailed

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "wsl")

        async def refuse(*_args: object, **_kwargs: object) -> dict:
            if remote:
                raise ClusterOpFailed({"error": "ResurrectRefused: runtime_cutover_required"})
            raise ResurrectRefused("runtime_cutover_required")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", refuse)
        assert (
            await lifecycle.resurrect_if_terminated(
                _db(database_gate=database_gate),
                event_bus,
                7,
                trigger_inbound_id=99,
                trigger_inbound_kind=InboundKind.CHAT,
            )
            is AgentStatus.TERMINATED
        )

    @pytest.mark.asyncio
    async def test_not_terminated_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.RUNNING)

        def _no_machine_read(_db, _aid: int) -> str:
            raise AssertionError("a live agent must not trigger a machine lookup")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)

        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=99,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.RUNNING


class TestResurrectIfTerminatedNotificationGuard:
    """A system-family chat trigger never resurrects its owner (user ruling
    2026-08-27; task #3687): the watcher-reap notice that woke 6260 twice is a
    queued notification, not a wake-up call. The guard reads the trigger row
    itself; a missing row falls through to the normal path (the home runner's
    final CAS still adjudicates stale work), and a DB read failure propagates
    instead of silently becoming a skip."""

    @pytest.fixture(autouse=True)
    def _default_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _db, _aid: False)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _db, _aid: False)
        monkeypatch.setattr(lifecycle, "_clear_wake_suppression", lambda _db, _aid: None)

    @pytest.mark.asyncio
    async def test_system_notice_trigger_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(
            lifecycle, "_system_notice_source_of_trigger", lambda _db, _aid, _iid: "system"
        )

        def _no_machine_read(_db, _aid: int) -> str:
            raise AssertionError("a system notice must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=207124,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_missing_trigger_row_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        """No row -> None -> the normal path runs; stale-work adjudication stays
        with the home runner's final CAS. This drives the real read (the id
        does not exist), not a stubbed one."""
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        calls: list[int] = []

        async def _fake_op(
            _db: object,
            _bus: object,
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            calls.append(agent_id)
            return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_op)

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            5,
            trigger_inbound_id=10**12,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.IDLING
        assert calls == [5]

    @pytest.mark.asyncio
    async def test_read_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, *, database_gate: ProcessDbGate
    ) -> None:
        """A failed trigger read must not be swallowed into a skip (review
        note A): the error surfaces to the caller, mirroring how a failed
        suppression / breaker read above fails loudly."""
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)

        def _boom(_db: object, _aid: int, _iid: int) -> str | None:
            raise RuntimeError("trigger read failed")

        monkeypatch.setattr(lifecycle, "_system_notice_source_of_trigger", _boom)
        with pytest.raises(RuntimeError, match="trigger read failed"):
            await lifecycle.resurrect_if_terminated(
                _db(database_gate=database_gate),
                event_bus,
                5,
                trigger_inbound_id=207124,
                trigger_inbound_kind=InboundKind.CHAT,
            )

    def test_trigger_guard_reads_row_kind_source_and_payload(
        self, db_conn: psycopg.Connection, database: Database, event_bus: EventBus
    ) -> None:
        """The guard reads the row itself: system-family chats are notices
        (plain and variant), a user chat and a system_note are not, and a
        missing row / foreign agent falls through to None. The payload marker
        is a fail-closed carve-out: only the exact JSON boolean `true` lets a
        system-family chat through — a missing key, null, or any other value
        (even the string "true") stays a notice (task #3687 review, Ava #3242)."""
        from base.db import create_agent, insert_inbound_message

        aid = create_agent(db_conn)
        db_conn.commit()
        sys_iid = insert_inbound_message(
            db_conn, aid, "notice", source="system", bus=event_bus, database=database
        )
        var_iid = insert_inbound_message(
            db_conn, aid, "variant", source="system:notice-reply", bus=event_bus, database=database
        )
        user_iid = insert_inbound_message(
            db_conn, aid, "hi", source="user", bus=event_bus, database=database
        )
        note_iid = insert_inbound_message(
            db_conn,
            aid,
            "note",
            source="system",
            kind="system_note",
            bus=event_bus,
            database=database,
        )
        # A watcher's wake is source="watcher:<id>" — neither "system" nor a
        # "system:" variant, so it is NOT a notice: a terminated owner with a
        # live watcher is auto-resurrected at its next fire, same as any user
        # chat (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md).
        watcher_iid = insert_inbound_message(
            db_conn, aid, "wake", source="watcher:7", bus=event_bus, database=database
        )

        assert lifecycle._system_notice_source_of_trigger(database, aid, sys_iid) == "system"
        assert (
            lifecycle._system_notice_source_of_trigger(database, aid, var_iid)
            == "system:notice-reply"
        )
        assert lifecycle._system_notice_source_of_trigger(database, aid, user_iid) is None
        assert lifecycle._system_notice_source_of_trigger(database, aid, note_iid) is None
        assert lifecycle._system_notice_source_of_trigger(database, aid, watcher_iid) is None
        assert lifecycle._system_notice_source_of_trigger(database, aid, 10**12) is None
        assert lifecycle._system_notice_source_of_trigger(database, aid + 999, sys_iid) is None

        recovery_iid = insert_inbound_message(
            db_conn,
            aid,
            "continue",
            source="system",
            payload={"hosted_turn_recovery": True},
            bus=event_bus,
            database=database,
        )
        assert lifecycle._system_notice_source_of_trigger(database, aid, recovery_iid) is None
        user_marker_iid = insert_inbound_message(
            db_conn,
            aid,
            "hi",
            source="user",
            payload={"hosted_turn_recovery": True},
            bus=event_bus,
            database=database,
        )
        assert lifecycle._system_notice_source_of_trigger(database, aid, user_marker_iid) is None

        fail_closed: tuple[tuple[str, dict[str, object] | None], ...] = (
            ("payload-absent", None),
            ("key-absent", {"content_blocks": []}),
            ("json-null", {"hosted_turn_recovery": None}),
            ("boolean-false", {"hosted_turn_recovery": False}),
            ("string-true", {"hosted_turn_recovery": "true"}),
            ("number-1", {"hosted_turn_recovery": 1}),
        )
        for label, payload in fail_closed:
            iid = insert_inbound_message(
                db_conn,
                aid,
                "noticed",
                source="system",
                payload=payload,
                bus=event_bus,
                database=database,
            )
            assert lifecycle._system_notice_source_of_trigger(database, aid, iid) == "system", label

    @pytest.mark.asyncio
    async def test_hosted_turn_recovery_marker_reaches_dispatch(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """BLOCK regression (Ava #3242): the watchdog's hosted-turn recovery
        chat is kind='chat', source='system' — the plain notice verdict
        silently matched it and both resurrection channels went dark. This
        drives the REAL guard (no stub) against the REAL marker row and
        asserts the chain reaches the resurrect dispatch, with only the
        below-dispatch machinery stubbed; a guard that wrongly matched would
        reach no dispatch and fail the assert."""
        from base.agents import AgentStatus
        from base.db import create_agent, insert_inbound_message

        aid = create_agent(db_conn)
        db_conn.commit()
        rec_iid = insert_inbound_message(
            db_conn,
            aid,
            "continue from the latest checkpoint",
            source="system",
            payload={"hosted_turn_recovery": True},
            bus=event_bus,
            database=database,
        )
        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _db, _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        calls: list[tuple[int, int | None]] = []

        async def _fake_op(
            _db: object,
            _bus: object,
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            calls.append((agent_id, trigger_inbound_id))
            return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_op)

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        status = await lifecycle.resurrect_if_terminated(
            _db(database_gate=database_gate),
            event_bus,
            aid,
            trigger_inbound_id=rec_iid,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        assert status is AgentStatus.IDLING
        assert calls == [(aid, rec_iid)]


@pytest.mark.asyncio
async def test_spawned_auto_resurrect_clears_suppression_in_database(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A successful spawn is a durable recovery, not only an in-memory result."""
    from base.agents import AgentStatus
    from base.db import create_agent, insert_inbound_message

    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta "
        "(id,status,machine,wake_suppressed_until,wake_suppress_reason) "
        "VALUES(%s,'terminated','remote-home',now()-interval '1 second','resurrect_failed')",
        (agent_id,),
    )
    db_conn.commit()
    trigger_id = insert_inbound_message(
        db_conn, agent_id, "recover", source="user", bus=event_bus, database=database
    )

    async def _spawn_on_home(*_args: object, **_kwargs: object) -> dict[str, str]:
        db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
        db_conn.commit()
        return {"status": "spawned"}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _spawn_on_home)

    status = await lifecycle.resurrect_if_terminated(
        _db(database_gate=database_gate),
        event_bus,
        agent_id,
        trigger_inbound_id=trigger_id,
        trigger_inbound_kind=InboundKind.CHAT,
    )

    assert status is AgentStatus.IDLING
    assert db_conn.execute(
        "SELECT wake_suppressed_until,wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (None, None)
