"""`recover-crash-marked-v2` — the stalled crash-marked harvest (task #3618).

Two layers: the home runner's adjudication (`_recover_crash_marked_blocking`,
one row-locked transaction that harvests a crash-marked idling corpse into the
corpse reaper's terminal shape or refuses with a reason) and the requester
(`recover_crash_marked_if_stalled`) the delivery watchdog calls — fail-closed,
never raising, returning `(decision, reason)`.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import psycopg
import pytest
from pydantic import ValidationError

from base.agents import AgentNotFound, CrashRecoveryResult
from base.cluster.machine import machine_name
from base.db import Database
from base.events.live.bus import EventBus
from base.telemetry import Event
from ops import cluster_rpc, lifecycle
from ops.agents import create_agent_row
from ops.lifecycle import CrashRecoveryRequestFailure, crash_harvest
from ops.rpc_schemas import RecoverCrashMarkedResponse


@pytest.mark.parametrize("result", list(CrashRecoveryResult))
def test_crash_recovery_wire_values_keep_the_domain_owner(result: CrashRecoveryResult) -> None:
    response = RecoverCrashMarkedResponse.model_validate({"status": result.value, "reason": None})
    assert response.status is result
    assert response.model_dump(mode="json") == {"status": result.value, "reason": None}
    schema = RecoverCrashMarkedResponse.model_json_schema()
    status = schema["properties"]["status"]
    owner = schema["$defs"][status["$ref"].split("/")[-1]]
    assert set(owner["enum"]) == {member.value for member in CrashRecoveryResult}


@pytest.mark.parametrize(
    "payload", [{}, {"status": "unknown"}, {"status": "error"}, {"status": "unreachable"}]
)
def test_runner_response_rejects_unknown_or_local_request_results(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RecoverCrashMarkedResponse.model_validate(payload)


class _Stubs(NamedTuple):
    events: list[dict[str, object]]
    published: list[int]


def _park_corpse(
    db: psycopg.Connection,
    *,
    status: str = "idling",
    marked: bool = True,
    machine: str | None = None,
    runtime_kind: str | None = "hosted",
    streak: int = 0,
    lease_seconds: float | None = None,
    suppress_reason: str | None = None,
    suppress_seconds: float | None = None,
) -> int:
    """A row shaped like a crash-marked corpse: `create_agent_row` leaves the
    death marker NULL (the production stamp is
    `agent.ownership.hosted.stamp_turn_fatal`), so the scenario sets the
    marker, the settle fields, and any suppression window explicitly."""
    aid, _birth, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(),
        EventBus.from_settings(),
        spawner="user",
        machine=machine or machine_name(),
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET "
            "status = %s, runtime_kind = %s, permanent_reject_streak = %s, "
            "last_turn_fatal_at = CASE WHEN %s::boolean THEN now() ELSE NULL END, "
            "lease_expires_at = CASE WHEN %s::float8 IS NULL THEN NULL "
            "  ELSE now() + make_interval(secs => %s::float8) END, "
            "wake_suppressed_until = CASE WHEN %s::float8 IS NULL THEN NULL "
            "  ELSE now() + make_interval(secs => %s::float8) END, "
            "wake_suppress_reason = %s "
            "WHERE id = %s",
            (
                status,
                runtime_kind,
                streak,
                marked,
                lease_seconds,
                lease_seconds,
                suppress_seconds,
                suppress_seconds,
                suppress_reason,
                aid,
            ),
        )
    db.commit()
    return aid


@pytest.fixture(autouse=True)
def stubs(monkeypatch: pytest.MonkeyPatch) -> _Stubs:
    """Capture the prepared audit and frontend publish without touching Redis."""
    events: list[dict[str, object]] = []
    published: list[int] = []
    prepare = crash_harvest.prepare_event_log

    def _record_event(
        *,
        event_type: str,
        agent_id: int | None,
        source: str,
        target_agent_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        events.append(
            {
                "event_type": event_type,
                "agent_id": agent_id,
                "source": source,
                "payload": payload or {},
            }
        )
        return prepare(
            event_type=event_type,
            agent_id=agent_id,
            source=source,
            target_agent_id=target_agent_id,
            payload=payload,
        )

    def _record_publish(_bus: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr(crash_harvest, "prepare_event_log", _record_event)
    monkeypatch.setattr(crash_harvest, "publish_agent_updated_sync", _record_publish)
    return _Stubs(events=events, published=published)


def _no_halt(_db: object, _aid: int) -> str | None:
    return None


def _halted_permanent(_db: object, _aid: int) -> str | None:
    return "permanent_provider_reject"


def _home_a(_db: Database, _aid: int) -> str:
    return "home-a"


def _gateway_box() -> str:
    return "gateway-box"


def _local_home_name() -> str:
    return "home-a"


class TestRecoverCrashMarkedOp:
    def test_harvests_a_marked_idling_corpse(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn)
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert response.status is CrashRecoveryResult.HARVESTED
        assert response.reason is None
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, termination_source, lease_expires_at, "
                "runtime_protocol_version, last_turn_fatal_at IS NOT NULL "
                "FROM agents_meta WHERE id = %s",
                (aid,),
            )
            row = cur.fetchone()
        assert row is not None
        assert tuple(row) == ("terminated", "reaper", None, 0, True)
        # The marker is KEPT (never cleared): the relaxed trigger must still
        # match the harvested row.
        assert stubs.events == [
            {
                "event_type": "status_change",
                "agent_id": aid,
                "source": "system",
                "payload": {"from": "idling", "to": "terminated", "reason": "corpse_reaper"},
            }
        ]
        assert stubs.published == [aid]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT source, attributes FROM audit_events "
                "WHERE agent_id = %s AND event_name = 'status_change'",
                (aid,),
            )
            recorded = cur.fetchall()
        assert recorded == [
            ("system", {"from": "idling", "to": "terminated", "reason": "corpse_reaper"})
        ]

    def test_repeat_call_is_idempotent(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn)
        assert (
            lifecycle._recover_crash_marked_blocking(database, event_bus, aid).status
            is CrashRecoveryResult.HARVESTED
        )
        second = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert second.status is CrashRecoveryResult.ALREADY_TERMINATED
        assert second.reason is None
        assert len(stubs.events) == 1  # no second harvest event

    def test_refuses_unmarked(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, marked=False)
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (CrashRecoveryResult.REFUSED, "not_marked")
        assert stubs.events == []

    def test_refuses_unsettled_running_owner(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, status="running")
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (
            CrashRecoveryResult.REFUSED,
            "not_settled:running",
        )

    def test_refuses_non_hosted_runtime(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, runtime_kind="process")
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (
            CrashRecoveryResult.REFUSED,
            "not_settled:runtime_kind=process",
        )

    def test_refuses_foreign_machine(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, machine="somewhere-else")
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (CrashRecoveryResult.REFUSED, "wrong_machine")

    def test_refuses_live_lease(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, lease_seconds=3600.0)
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (CrashRecoveryResult.REFUSED, "lease_alive")

    def test_refuses_while_wake_suppressed(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        """An active suppression window (without a tripped breaker) still
        refuses: automatic recovery is halted until the window expires."""
        aid = _park_corpse(
            db_conn, suppress_reason="permanent_provider_reject", suppress_seconds=3600.0
        )
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (
            CrashRecoveryResult.REFUSED,
            "permanent_provider_reject",
        )
        assert stubs.events == []

    def test_refuses_when_the_recovery_breaker_tripped(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        """The durable streak gate holds even with no suppression window: a
        claim that cleared the window must not unlock a halted agent."""
        aid = _park_corpse(db_conn, streak=2)
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (
            CrashRecoveryResult.REFUSED,
            "permanent_provider_reject",
        )
        assert stubs.events == []

    def test_refuses_with_fallback_reason_for_reasonless_window(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        aid = _park_corpse(db_conn, suppress_seconds=3600.0)
        response = lifecycle._recover_crash_marked_blocking(database, event_bus, aid)
        assert (response.status, response.reason) == (
            CrashRecoveryResult.REFUSED,
            "wake_suppressed",
        )

    def test_missing_agent_raises(
        self, db_conn: psycopg.Connection, stubs: _Stubs, database: Database, event_bus: EventBus
    ) -> None:
        with pytest.raises(AgentNotFound):
            lifecycle._recover_crash_marked_blocking(database, event_bus, 10**9)


class TestRecoverCrashMarkedRequester:
    @pytest.mark.asyncio
    async def test_suppressed_short_circuits_before_any_rpc(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _halted_permanent)

        def _no_machine_read(_db: Database, _aid: int) -> str:
            raise AssertionError("a suppressed requester must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert (decision, reason) == (CrashRecoveryResult.REFUSED, "permanent_provider_reject")

    @pytest.mark.asyncio
    async def test_forwards_to_home_and_maps_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(lifecycle, "get_agent_machine", _home_a)
        seen: list[dict[str, object]] = []

        async def _dispatch(
            _db: object, *, target_machine: str, kind: str, payload: dict[str, object]
        ) -> dict[str, object]:
            seen.append({"target_machine": target_machine, "kind": kind, "payload": payload})
            return {"status": "harvested", "reason": None}

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _dispatch)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert (decision, reason) == (CrashRecoveryResult.HARVESTED, None)
        assert seen == [
            {
                "target_machine": "home-a",
                "kind": "lifecycle",
                "payload": {"path": "/api/agents/7/recover-crash-marked-v2", "body": {}},
            }
        ]

    @pytest.mark.asyncio
    async def test_local_home_falls_back_in_process(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(lifecycle, "get_agent_machine", _home_a)
        monkeypatch.setattr(lifecycle, "machine_name", _local_home_name)
        calls: list[int] = []

        async def _local_op(_db: object, _bus: object, agent_id: int) -> RecoverCrashMarkedResponse:
            calls.append(agent_id)
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason="not_settled:running"
            )

        # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(lifecycle, "recover_crash_marked_op", _local_op)

        async def _unreachable(_db: object, **_kwargs: object) -> dict[str, object]:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert (decision, reason) == (CrashRecoveryResult.REFUSED, "not_settled:running")
        assert calls == [7]

    @pytest.mark.asyncio
    async def test_remote_home_unreachable_reports_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(lifecycle, "get_agent_machine", _home_a)
        monkeypatch.setattr(lifecycle, "machine_name", _gateway_box)

        async def _unreachable(_db: object, **_kwargs: object) -> dict[str, object]:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("connect timeout")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert decision is CrashRecoveryRequestFailure.UNREACHABLE
        assert reason == "connect timeout"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload", [{}, {"status": "unknown"}, {"status": "unreachable"}, {"status": "error"}]
    )
    async def test_invalid_runner_reply_is_local_error(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus, payload: dict[str, object]
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(lifecycle, "get_agent_machine", _home_a)

        async def invalid_reply(_db: object, **_kwargs: object) -> dict[str, object]:
            return payload

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", invalid_reply)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert decision is CrashRecoveryRequestFailure.ERROR
        assert reason == "harvest request failed"

    @pytest.mark.asyncio
    async def test_unexpected_failure_maps_to_error(
        self, monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
    ) -> None:
        monkeypatch.setattr(lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(lifecycle, "get_agent_machine", _home_a)

        async def _explode(_db: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("kaput")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _explode)
        decision, reason = await lifecycle.recover_crash_marked_if_stalled(
            Database.from_settings(), event_bus, 7, stalled_inbound_id=88
        )
        assert decision is CrashRecoveryRequestFailure.ERROR
        assert reason == "harvest request failed"


@pytest.mark.asyncio
async def test_lifecycle_op_dispatches_the_recover_path(
    monkeypatch: pytest.MonkeyPatch, database: Database, event_bus: EventBus
) -> None:
    calls: list[int] = []

    async def _fake_op(_db: object, _bus: object, agent_id: int) -> RecoverCrashMarkedResponse:
        calls.append(agent_id)
        return RecoverCrashMarkedResponse(status=CrashRecoveryResult.HARVESTED)

    # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(lifecycle, "recover_crash_marked_op", _fake_op)
    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        "/api/agents/42/recover-crash-marked-v2",
        {},
        object(),  # type: ignore[arg-type]
    )
    assert isinstance(result, RecoverCrashMarkedResponse)
    assert result.status is CrashRecoveryResult.HARVESTED
    assert calls == [42]
