"""`recover-crash-marked-v2` — the stalled crash-marked harvest (task #3618).

Two layers: the home runner's adjudication (`_recover_crash_marked_blocking`,
one row-locked transaction that harvests a crash-marked idling corpse into the
corpse reaper's terminal shape or refuses with a reason) and the requester
(`recover_crash_marked_if_stalled`) the delivery watchdog calls — fail-closed,
never raising, returning `(decision, reason)`.
"""

from __future__ import annotations

from typing import NamedTuple

import psycopg
import pytest

from ops import ops_lifecycle
from ops.agent_spawn import create_agent_row
from ops.rpc_schemas import RecoverCrashMarkedResponse
from shared.agents import AgentNotFound
from shared.machine import machine_name


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
    `agent/hosted_ownership.stamp_turn_fatal`), so the scenario sets the
    marker, the settle fields, and any suppression window explicitly."""
    aid, _birth = create_agent_row(spawner="user", machine=machine or machine_name())
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
    """Capture the op's non-transactional side effects: the audit event enqueue
    and the frontend snapshot publish (never touch Redis from these tests)."""
    events: list[dict[str, object]] = []
    published: list[int] = []

    def _record_event(**kwargs: object) -> None:
        events.append(kwargs)

    def _record_publish(agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr(ops_lifecycle, "insert_event_log", _record_event)
    monkeypatch.setattr(ops_lifecycle, "publish_agent_updated_sync", _record_publish)
    return _Stubs(events=events, published=published)


def _no_halt(_aid: int) -> str | None:
    return None


def _halted_permanent(_aid: int) -> str | None:
    return "permanent_provider_reject"


def _home_a(_aid: int) -> str:
    return "home-a"


def _gateway_box() -> str:
    return "gateway-box"


def _local_home_name() -> str:
    return "home-a"


class TestRecoverCrashMarkedOp:
    def test_harvests_a_marked_idling_corpse(
        self, db_conn: psycopg.Connection, stubs: _Stubs
    ) -> None:
        aid = _park_corpse(db_conn)
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert response.status == "harvested"
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

    def test_repeat_call_is_idempotent(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        aid = _park_corpse(db_conn)
        assert ops_lifecycle._recover_crash_marked_blocking(aid).status == "harvested"
        second = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert second.status == "already_terminated"
        assert second.reason is None
        assert len(stubs.events) == 1  # no second harvest event

    def test_refuses_unmarked(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        aid = _park_corpse(db_conn, marked=False)
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "not_marked")
        assert stubs.events == []

    def test_refuses_unsettled_running_owner(
        self, db_conn: psycopg.Connection, stubs: _Stubs
    ) -> None:
        aid = _park_corpse(db_conn, status="running")
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "not_settled:running")

    def test_refuses_non_hosted_runtime(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        aid = _park_corpse(db_conn, runtime_kind="process")
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "not_settled:runtime_kind=process")

    def test_refuses_foreign_machine(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        aid = _park_corpse(db_conn, machine="somewhere-else")
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "wrong_machine")

    def test_refuses_live_lease(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        aid = _park_corpse(db_conn, lease_seconds=3600.0)
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "lease_alive")

    def test_refuses_while_wake_suppressed(
        self, db_conn: psycopg.Connection, stubs: _Stubs
    ) -> None:
        """An active suppression window (without a tripped breaker) still
        refuses: automatic recovery is halted until the window expires."""
        aid = _park_corpse(
            db_conn, suppress_reason="permanent_provider_reject", suppress_seconds=3600.0
        )
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "permanent_provider_reject")
        assert stubs.events == []

    def test_refuses_when_the_recovery_breaker_tripped(
        self, db_conn: psycopg.Connection, stubs: _Stubs
    ) -> None:
        """The durable streak gate holds even with no suppression window: a
        claim that cleared the window must not unlock a halted agent."""
        aid = _park_corpse(db_conn, streak=2)
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "permanent_provider_reject")
        assert stubs.events == []

    def test_refuses_with_fallback_reason_for_reasonless_window(
        self, db_conn: psycopg.Connection, stubs: _Stubs
    ) -> None:
        aid = _park_corpse(db_conn, suppress_seconds=3600.0)
        response = ops_lifecycle._recover_crash_marked_blocking(aid)
        assert (response.status, response.reason) == ("refused", "wake_suppressed")

    def test_missing_agent_raises(self, db_conn: psycopg.Connection, stubs: _Stubs) -> None:
        with pytest.raises(AgentNotFound):
            ops_lifecycle._recover_crash_marked_blocking(10**9)


class TestRecoverCrashMarkedRequester:
    @pytest.mark.asyncio
    async def test_suppressed_short_circuits_before_any_rpc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ops_lifecycle, "_recovery_halt_reason", _halted_permanent)

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("a suppressed requester must not read or contact the home")

        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _no_machine_read)
        decision, reason = await ops_lifecycle.recover_crash_marked_if_stalled(
            7, stalled_inbound_id=88
        )
        assert (decision, reason) == ("refused", "permanent_provider_reject")

    @pytest.mark.asyncio
    async def test_forwards_to_home_and_maps_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ops_lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _home_a)
        seen: list[dict[str, object]] = []

        async def _dispatch(
            *, target_machine: str, kind: str, payload: dict[str, object]
        ) -> dict[str, object]:
            seen.append({"target_machine": target_machine, "kind": kind, "payload": payload})
            return {"status": "harvested", "reason": None}

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _dispatch)
        decision, reason = await ops_lifecycle.recover_crash_marked_if_stalled(
            7, stalled_inbound_id=88
        )
        assert (decision, reason) == ("harvested", None)
        assert seen == [
            {
                "target_machine": "home-a",
                "kind": "lifecycle",
                "payload": {"path": "/api/agents/7/recover-crash-marked-v2", "body": {}},
            }
        ]

    @pytest.mark.asyncio
    async def test_local_home_falls_back_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ops_lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _home_a)
        monkeypatch.setattr(ops_lifecycle, "machine_name", _local_home_name)
        calls: list[int] = []

        async def _local_op(agent_id: int) -> RecoverCrashMarkedResponse:
            calls.append(agent_id)
            return RecoverCrashMarkedResponse(status="refused", reason="not_settled:running")

        monkeypatch.setattr(ops_lifecycle, "recover_crash_marked_op", _local_op)

        async def _unreachable(**_kwargs: object) -> dict[str, object]:
            raise ops_lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _unreachable)
        decision, reason = await ops_lifecycle.recover_crash_marked_if_stalled(
            7, stalled_inbound_id=88
        )
        assert (decision, reason) == ("refused", "not_settled:running")
        assert calls == [7]

    @pytest.mark.asyncio
    async def test_remote_home_unreachable_reports_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ops_lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _home_a)
        monkeypatch.setattr(ops_lifecycle, "machine_name", _gateway_box)

        async def _unreachable(**_kwargs: object) -> dict[str, object]:
            raise ops_lifecycle._cluster_rpc.ClusterOpUnreachable("connect timeout")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _unreachable)
        decision, reason = await ops_lifecycle.recover_crash_marked_if_stalled(
            7, stalled_inbound_id=88
        )
        assert decision == "unreachable"
        assert reason == "connect timeout"

    @pytest.mark.asyncio
    async def test_unexpected_failure_maps_to_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ops_lifecycle, "_recovery_halt_reason", _no_halt)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _home_a)

        async def _explode(**_kwargs: object) -> dict[str, object]:
            raise RuntimeError("kaput")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _explode)
        decision, reason = await ops_lifecycle.recover_crash_marked_if_stalled(
            7, stalled_inbound_id=88
        )
        assert (decision, reason) == ("error", "harvest request failed")


@pytest.mark.asyncio
async def test_lifecycle_op_dispatches_the_recover_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def _fake_op(agent_id: int) -> RecoverCrashMarkedResponse:
        calls.append(agent_id)
        return RecoverCrashMarkedResponse(status="harvested")

    monkeypatch.setattr(ops_lifecycle, "recover_crash_marked_op", _fake_op)
    result = await ops_lifecycle.lifecycle_op(
        "/api/agents/42/recover-crash-marked-v2",
        {},
        object(),  # type: ignore[arg-type]
    )
    assert isinstance(result, RecoverCrashMarkedResponse)
    assert result.status == "harvested"
    assert calls == [42]
