"""Gateway-side hosted-turn liveness detection and recovery."""

from __future__ import annotations

import asyncio
import json

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.delivery_watchdog import daemon as delivery_daemon
from services.delivery_watchdog import turn_liveness as watchdog
from shared.config import settings

_THRESHOLD_S = 2400.0


@pytest.fixture
def pool():
    db_pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield db_pool
    finally:
        db_pool.close()


def _make_hosted_running_agent(
    db: psycopg.Connection,
    *,
    machine: str = "runner-a",
    age_s: float = _THRESHOLD_S + 60.0,
) -> int:
    from tests.conftest import spawn_agent

    agent_id = spawn_agent(spawner="user")
    db.execute(
        "UPDATE agents_meta SET status='running', runtime_kind='hosted', machine=%s, "
        "last_active_at=now() - make_interval(secs => %s) WHERE id=%s",
        (machine, age_s, agent_id),
    )
    db.commit()
    return agent_id


class FakeRedis:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values

    async def get(self, key: str) -> str | None:
        return self.values[key]


def test_gateway_reads_hosted_turn_threshold_from_current_config_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delivery_daemon,
        "current_field_values",
        lambda: {"wedged_agent_inbound_age_seconds": 2500.0},
    )

    assert delivery_daemon._hosted_turn_threshold_seconds() == 2500.0


def test_select_hosted_turn_candidates_uses_db_wall_clock_and_exact_runtime_state(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
) -> None:
    stale_hosted = _make_hosted_running_agent(db_conn)
    fresh_hosted = _make_hosted_running_agent(db_conn, age_s=_THRESHOLD_S - 1.0)
    process_agent = _make_hosted_running_agent(db_conn)
    idling_hosted = _make_hosted_running_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET runtime_kind='process' WHERE id=%s", (process_agent,))
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (idling_hosted,))
    db_conn.commit()

    candidates = watchdog.select_hosted_turn_liveness_candidates(pool, _THRESHOLD_S)

    assert [candidate.agent_id for candidate in candidates] == [stale_hosted]
    assert candidates[0].machine == "runner-a"
    assert candidates[0].db_age_s >= _THRESHOLD_S
    assert fresh_hosted not in {candidate.agent_id for candidate in candidates}


async def test_live_progress_prevents_recovery_after_db_age_exceeds_threshold(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
) -> None:
    """A long turn remains healthy beyond 2400s while node/chunk marks stay fresh."""
    agent_id = _make_hosted_running_agent(db_conn, age_s=_THRESHOLD_S + 600.0)
    redis = FakeRedis(
        {
            "host_turn_progress:runner-a": json.dumps(
                {str(agent_id): {"age_s": 5.0, "last_marks": [10.0, 20.0, 30.0]}}
            )
        }
    )

    wedges = await watchdog._detect_hosted_turn_wedges(pool, _THRESHOLD_S, redis)

    assert wedges == []


@pytest.mark.parametrize(
    ("heartbeat", "expected_age", "expected_marks", "heartbeat_missing"),
    [
        (None, _THRESHOLD_S + 60.0, (), True),
        (
            json.dumps({"{agent_id}": {"age_s": _THRESHOLD_S + 1.0, "last_marks": [1.0, 2.0]}}),
            _THRESHOLD_S + 1.0,
            (1.0, 2.0),
            False,
        ),
    ],
)
async def test_missing_or_stale_host_progress_is_a_wedge(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    heartbeat: str | None,
    expected_age: float,
    expected_marks: tuple[float, ...],
    heartbeat_missing: bool,
) -> None:
    agent_id = _make_hosted_running_agent(db_conn)
    if heartbeat is not None:
        heartbeat = heartbeat.replace("{agent_id}", str(agent_id))
    redis = FakeRedis({"host_turn_progress:runner-a": heartbeat})

    wedges = await watchdog._detect_hosted_turn_wedges(pool, _THRESHOLD_S, redis)

    assert len(wedges) == 1
    assert wedges[0].agent_id == agent_id
    assert wedges[0].age_s == pytest.approx(expected_age, abs=1.0)  # pyright: ignore[reportUnknownMemberType]
    assert wedges[0].last_marks == expected_marks
    assert wedges[0].heartbeat_missing is heartbeat_missing


async def test_invalid_host_progress_cannot_authorize_recovery(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
) -> None:
    agent_id = _make_hosted_running_agent(db_conn)
    redis = FakeRedis(
        {
            "host_turn_progress:runner-a": json.dumps(
                {str(agent_id): {"age_s": float("inf"), "last_marks": [1.0]}}
            )
        }
    )

    wedges = await watchdog._detect_hosted_turn_wedges(pool, _THRESHOLD_S, redis)

    assert wedges == []


def test_recovery_queue_creates_durable_marked_pending_system_chat(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
) -> None:
    agent_id = _make_hosted_running_agent(db_conn)

    trigger_id = watchdog._queue_hosted_turn_recovery(pool, agent_id)

    row = db_conn.execute(
        "SELECT kind, source, status, content, payload FROM inbound_messages WHERE id=%s",
        (trigger_id,),
    ).fetchone()
    assert row == (
        "chat",
        "system",
        "pending",
        "Your previous hosted turn stopped making progress and was restarted "
        "by the delivery watchdog. Continue from the latest checkpoint.",
        # The marker is the wire contract that lets this system-source chat
        # through the notice guard on both resurrection channels (task #3687
        # review, Ava #3242).
        {"hosted_turn_recovery": True},
    )


async def test_recovery_emits_evidence_then_terminates_queues_and_resurrects(
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ops.ops_lifecycle as lifecycle

    calls: list[str] = []

    def fake_emit(*args: object, **kwargs: object) -> None:
        calls.append("event")
        assert args[:2] == ("telemetry", "host_turn_stall_detected")

    async def fake_terminate(agent_id: int, body: object, db_pool: object) -> object:
        calls.append("terminate")
        assert agent_id == 42
        assert body.force is True  # type: ignore[attr-defined]
        assert body.source == "system"  # type: ignore[attr-defined]
        assert db_pool is pool
        return object()

    def fake_queue(db_pool: object, agent_id: int) -> int:
        calls.append("queue")
        assert db_pool is pool
        assert agent_id == 42
        return 9001

    async def fake_resurrect(
        agent_id: int,
        *,
        trigger_inbound_id: int,
        trigger_inbound_kind: str,
    ) -> str:
        calls.append("resurrect")
        assert (agent_id, trigger_inbound_id) == (42, 9001)
        assert trigger_inbound_kind == "chat"
        return "idling"

    monkeypatch.setattr(watchdog.telemetry, "emit", fake_emit)
    monkeypatch.setattr(lifecycle, "terminate_agent_op", fake_terminate)
    monkeypatch.setattr(watchdog, "_queue_hosted_turn_recovery", fake_queue)
    monkeypatch.setattr(lifecycle, "resurrect_if_terminated", fake_resurrect)
    wedge = watchdog._HostedTurnWedge(42, "runner-a", 2500.0, (1.0, 2.0, 3.0), False)

    await watchdog._recover_hosted_turn(pool, wedge)

    assert calls == ["event", "terminate", "queue", "resurrect"]


async def test_recovery_chain_reaches_dispatch_through_the_real_notice_guard(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCK regression (Ava #3242): the queued recovery trigger is
    source='system', so the plain notice guard used to cut the chain before
    any resurrect ran. This drives the REAL `resurrect_if_terminated`, the
    REAL guard read, and the REAL queued marker row; only the terminate and
    the below-dispatch machinery are stubbed — a guard that wrongly matched
    would reach no dispatch and fail the assert."""
    import ops.ops_lifecycle as lifecycle

    agent_id = _make_hosted_running_agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated', status_changed_at=now() WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    def _silent_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(watchdog.telemetry, "emit", _silent_emit)

    async def fake_terminate(agent_id: int, body: object, db_pool: object) -> object:
        return object()

    monkeypatch.setattr(lifecycle, "terminate_agent_op", fake_terminate)
    dispatched: list[dict[str, object]] = []

    async def fake_dispatch(
        *, target_machine: str, kind: str, payload: dict[str, object]
    ) -> dict[str, str]:
        dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})
        return {"status": "spawned"}

    monkeypatch.setattr(lifecycle._cluster_rpc, "dispatch_to_machine", fake_dispatch)

    wedge = watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), False)
    await watchdog._recover_hosted_turn(pool, wedge)

    row = db_conn.execute(
        "SELECT id, payload FROM inbound_messages "
        "WHERE agent_id=%s AND source='system' ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    trigger_id, payload = row
    assert payload == {"hosted_turn_recovery": True}
    assert len(dispatched) == 1
    event = dispatched[0]
    assert event["target_machine"] == "runner-a"
    assert event["kind"] == "lifecycle"
    forwarded = event["payload"]
    assert isinstance(forwarded, dict)
    assert forwarded["path"] == f"/api/agents/{agent_id}/resurrect-if-pending-work-v2"
    assert forwarded["trigger_inbound_id"] == trigger_id
    assert forwarded["trigger_inbound_kind"] == "chat"
    body = forwarded["body"]
    assert isinstance(body, dict)
    assert body["resurrected_by"] == "system"


async def test_closed_agent_blocks_the_real_recovery_chain(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closure outranks the recovery carve-out too: a closed agent's
    wedged-turn recovery queues the marked chat (durable) but reaches no
    dispatch — the REAL guard reads the real marker and refuses, even though
    the same chat revives an open agent."""
    import ops.ops_lifecycle as lifecycle

    agent_id = _make_hosted_running_agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated', status_changed_at=now(), closed_at=now() "
        "WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    def _silent_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(watchdog.telemetry, "emit", _silent_emit)

    async def fake_terminate(agent_id: int, body: object, db_pool: object) -> object:
        return object()

    monkeypatch.setattr(lifecycle, "terminate_agent_op", fake_terminate)
    dispatched: list[dict[str, object]] = []

    async def fake_dispatch(
        *, target_machine: str, kind: str, payload: dict[str, object]
    ) -> dict[str, str]:
        dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})
        return {"status": "spawned"}

    monkeypatch.setattr(lifecycle._cluster_rpc, "dispatch_to_machine", fake_dispatch)

    wedge = watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), False)
    await watchdog._recover_hosted_turn(pool, wedge)

    assert dispatched == []
    # The marked recovery chat is still queued (durable), so an explicit
    # manual resurrect later picks the work up.
    row = db_conn.execute(
        "SELECT payload FROM inbound_messages WHERE agent_id=%s AND source='system' "
        "ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None and row[0] == {"hosted_turn_recovery": True}


async def test_hosted_turn_recovery_has_a_ten_minute_per_agent_cooldown(
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_recover(db_pool: object, wedge: object) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(watchdog, "_recover_hosted_turn", fake_recover)
    # A first attempt must run even during the machine's first ten minutes;
    # cooldown applies only when this process has an actual prior timestamp.
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 100.0)
    watchdog._last_hosted_turn_recovery_attempt.clear()
    watchdog._hosted_turn_recovery_tasks.clear()
    wedge = watchdog._HostedTurnWedge(42, "runner-a", 2500.0, (), True)

    try:
        watchdog._maybe_spawn_hosted_turn_recoveries(pool, [wedge])
        await started.wait()
        watchdog._maybe_spawn_hosted_turn_recoveries(pool, [wedge])
        assert calls == 1
        release.set()
        await asyncio.gather(*watchdog._hosted_turn_recovery_tasks.values())
        await asyncio.sleep(0)
        watchdog._maybe_spawn_hosted_turn_recoveries(pool, [wedge])
        assert calls == 1
        assert watchdog._last_hosted_turn_recovery_attempt[42] == 100.0
        assert watchdog.HOSTED_TURN_RECOVERY_COOLDOWN_S == 600.0
    finally:
        release.set()
        await asyncio.gather(*watchdog._hosted_turn_recovery_tasks.values(), return_exceptions=True)
        watchdog._hosted_turn_recovery_tasks.clear()
        watchdog._last_hosted_turn_recovery_attempt.clear()
