"""Gateway-side hosted-turn liveness detection and recovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import LiteralString

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.delivery_watchdog import attempts, resurrect_retry, rounds
from services.delivery_watchdog import daemon as delivery_daemon
from services.delivery_watchdog import turn_liveness as watchdog

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
    from tests.fixtures.units import spawn_agent

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
        watchdog,
        "current_field_values",
        lambda: {"wedged_agent_inbound_age_seconds": 2500.0},
    )

    assert watchdog.hosted_turn_threshold_seconds() == 2500.0


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


class _ProcessKilled(BaseException):
    """Stands in for the watchdog process dying: not an `Exception`, so no handler runs."""


def _silence_recovery_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real terminate op; drop its telemetry and the loopback host dial."""

    async def _host_unreachable(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no agent host in this test")

    def _silent_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(watchdog.telemetry, "emit", _silent_emit)
    monkeypatch.setattr(httpx.AsyncClient, "post", _host_unreachable)


def _stub_resurrect(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub only the home-runner resurrect; returns the trigger ids it received."""
    from ops import lifecycle

    triggers: list[int] = []

    async def _resurrect(
        _db: object, agent_id: int, *, trigger_inbound_id: int, trigger_inbound_kind: str
    ) -> str:
        triggers.append(trigger_inbound_id)
        return "idling"

    monkeypatch.setattr(lifecycle, "resurrect_if_terminated", _resurrect)
    return triggers


def _recovery_wakes(db: psycopg.Connection, agent_id: int) -> list[tuple[int, str, str]]:
    rows = db.execute(
        "SELECT id, status, content FROM inbound_messages "
        "WHERE agent_id=%s AND kind='chat' AND payload @> %s ORDER BY id",
        (agent_id, Jsonb({"hosted_turn_recovery": True})),
    ).fetchall()
    db.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def _row(db: psycopg.Connection, sql: LiteralString, agent_id: int) -> tuple[object, ...]:
    row = db.execute(sql, (agent_id,)).fetchone()
    db.commit()
    assert row is not None
    return row


async def test_recovery_commits_the_marked_wake_with_the_termination(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _make_hosted_running_agent(db_conn)
    _silence_recovery_side_effects(monkeypatch)
    triggers = _stub_resurrect(monkeypatch)

    await watchdog._recover_hosted_turn(
        pool,
        Database.from_settings(),
        watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), True),
    )

    wakes = _recovery_wakes(db_conn, agent_id)
    assert [(status, content) for _, status, content in wakes] == [
        (
            "pending",
            "Your previous hosted turn stopped making progress and was restarted "
            "by the delivery watchdog. Continue from the latest checkpoint.",
        )
    ]
    wake_id = wakes[0][0]
    row = db_conn.execute(
        "SELECT kind, source, payload FROM inbound_messages WHERE id=%s", (wake_id,)
    ).fetchone()
    db_conn.commit()
    # The marker is the wire contract that lets this system-source chat
    # through the notice guard on both resurrection channels (task #3687
    # review, Ava #3242).
    assert row == ("chat", "system", {"hosted_turn_recovery": True})
    assert triggers == [wake_id]


async def test_recovery_emits_evidence_then_terminates_with_its_wake_and_resurrects(
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops import lifecycle

    calls: list[str] = []

    def fake_emit(*args: object, **kwargs: object) -> None:
        calls.append("event")
        assert args[:2] == ("telemetry", "host_turn_stall_detected")

    async def fake_terminate(
        agent_id: int, body: object, db_pool: object, *, recovery_wake: str
    ) -> object:
        calls.append("terminate")
        assert agent_id == 42
        assert body.force is True  # type: ignore[attr-defined]
        assert body.source == "system"  # type: ignore[attr-defined]
        assert db_pool is pool
        assert recovery_wake == watchdog.HOSTED_TURN_RECOVERY_WAKE_TEXT
        return object()

    def fake_trigger(db_pool: object, agent_id: int) -> int:
        calls.append("trigger")
        assert db_pool is pool
        assert agent_id == 42
        return 9001

    async def fake_resurrect(
        _db: object,
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
    monkeypatch.setattr(watchdog, "_recovery_trigger", fake_trigger)
    monkeypatch.setattr(lifecycle, "resurrect_if_terminated", fake_resurrect)
    wedge = watchdog._HostedTurnWedge(42, "runner-a", 2500.0, (1.0, 2.0, 3.0), False)

    await watchdog._recover_hosted_turn(pool, Database.from_settings(), wedge)

    assert calls == ["event", "terminate", "trigger", "resurrect"]


async def test_recovery_chain_reaches_dispatch_through_the_real_notice_guard(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCK regression (Ava #3242): the queued recovery trigger is
    source='system', so the plain notice guard used to cut the chain before
    any resurrect ran. This drives the REAL terminate (which queues the
    marker row), the REAL `resurrect_if_terminated` and the REAL guard read;
    only the below-dispatch machinery is stubbed — a guard that wrongly
    matched would reach no dispatch and fail the assert."""
    from ops import cluster_rpc

    agent_id = _make_hosted_running_agent(db_conn)
    _silence_recovery_side_effects(monkeypatch)
    dispatched: list[dict[str, object]] = []

    async def fake_dispatch(
        _db: object, *, target_machine: str, kind: str, payload: dict[str, object]
    ) -> dict[str, str]:
        dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})
        return {"status": "spawned"}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", fake_dispatch)

    wedge = watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), False)
    await watchdog._recover_hosted_turn(pool, Database.from_settings(), wedge)

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


@pytest.mark.parametrize("failure", ["process_killed_after_terminate", "wake_insert_raises"])
async def test_no_failure_after_the_termination_commit_strands_the_agent(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The force terminate used to commit alone and the wake was queued by a
    separate later step; a process death or a failed insert between them left
    the agent `terminated` with nothing pending, and nothing in the watchdog
    ever looked at it again (the wedge scan selects only `running` rows, the
    terminated-owner retry only owners with a pending trigger). The wake now
    commits with the termination, so whatever happens after it, the agent is
    terminated WITH a trigger the terminated-owner retry resurrects."""
    import base.db
    from ops import lifecycle

    agent_id = _make_hosted_running_agent(db_conn)
    _silence_recovery_side_effects(monkeypatch)
    triggers = _stub_resurrect(monkeypatch)
    if failure == "process_killed_after_terminate":

        def _killed(*_args: object, **_kwargs: object) -> None:
            raise _ProcessKilled

        # The first thing the terminate op does once its transaction committed.
        monkeypatch.setattr(lifecycle, "publish_agent_updated_sync", _killed)
    else:

        def _insert_fails(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("injected wake insert failure")

        monkeypatch.setattr(base.db, "insert_inbound_message", _insert_fails)

    with contextlib.suppress(_ProcessKilled):
        await watchdog._recover_hosted_turn(
            pool,
            Database.from_settings(),
            watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), True),
        )

    assert _row(db_conn, "SELECT status FROM agents_meta WHERE id=%s", agent_id) == ("terminated",)
    wakes = _recovery_wakes(db_conn, agent_id)
    assert len(wakes) == 1
    owners = dict(delivery_daemon.select_terminated_owners_with_pending(pool, 86400.0))
    assert owners[agent_id] == wakes[0][0]
    # The watchdog's existing terminated-owner retry takes it from here.
    await resurrect_retry.resurrect_round(
        pool,
        Database.from_settings(),
        LoopProgress("resurrect", rounds.loop_liveness_timeout_s()),
        5,
        86400.0,
    )
    assert set(triggers) == {wakes[0][0]}


async def test_a_committed_recovery_is_never_recovered_twice(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency by state: the recovery's terminate takes the row out of the
    `running` set the wedge scan selects from, so a re-scan finds nothing to
    recover and the single wake is never duplicated."""
    agent_id = _make_hosted_running_agent(db_conn)
    _silence_recovery_side_effects(monkeypatch)
    _stub_resurrect(monkeypatch)
    await watchdog._recover_hosted_turn(
        pool,
        Database.from_settings(),
        watchdog._HostedTurnWedge(agent_id, "runner-a", 2500.0, (), True),
    )

    rescan = await watchdog._detect_hosted_turn_wedges(
        pool, _THRESHOLD_S, FakeRedis({"host_turn_progress:runner-a": None})
    )

    assert rescan == []
    assert len(_recovery_wakes(db_conn, agent_id)) == 1


def _wedged_runner_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runner-a's host beat is missing: every stale hosted agent on it is a wedge."""

    def async_redis(_bus: EventBus) -> FakeRedis:
        return FakeRedis({"host_turn_progress:runner-a": None})

    monkeypatch.setattr(EventBus, "async_redis", async_redis)


def _expire_recovery_cooldown(db: psycopg.Connection, agent_id: int) -> None:
    db.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '1 hour' "
        "WHERE kind = 'hosted_turn' AND agent_id = %s",
        (agent_id,),
    )
    db.commit()


async def test_hosted_turn_recovery_has_a_persisted_ten_minute_per_agent_cooldown(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock is a database row, so a watchdog restart resumes the cooldown
    instead of recovering the same agent again at once."""
    agent_id = _make_hosted_running_agent(db_conn)
    _wedged_runner_redis(monkeypatch)
    recovered: list[int] = []

    async def fake_recover(db_pool: object, _db: object, wedge: watchdog._HostedTurnWedge) -> None:
        recovered.append(wedge.agent_id)

    monkeypatch.setattr(watchdog, "_recover_hosted_turn", fake_recover)
    progress = LoopProgress("hosted_turn", rounds.loop_liveness_timeout_s())

    await watchdog.hosted_turn_recovery_round(
        pool, Database.from_settings(), EventBus.from_settings(), progress, _THRESHOLD_S
    )
    await watchdog.hosted_turn_recovery_round(
        pool, Database.from_settings(), EventBus.from_settings(), progress, _THRESHOLD_S
    )
    assert recovered == [agent_id]
    assert watchdog.HOSTED_TURN_RECOVERY_COOLDOWN_S == 600.0

    _expire_recovery_cooldown(db_conn, agent_id)
    await watchdog.hosted_turn_recovery_round(
        pool, Database.from_settings(), EventBus.from_settings(), progress, _THRESHOLD_S
    )
    assert recovered == [agent_id, agent_id]


async def test_a_hung_recovery_is_cut_at_the_deadline_and_still_enters_the_cooldown(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _make_hosted_running_agent(db_conn)
    _wedged_runner_redis(monkeypatch)

    async def hang(db_pool: object, _db: object, wedge: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(watchdog, "_recover_hosted_turn", hang)
    monkeypatch.setattr(rounds, "rpc_deadline_s", lambda: 0.05)

    await watchdog.hosted_turn_recovery_round(
        pool,
        Database.from_settings(),
        EventBus.from_settings(),
        LoopProgress("hosted_turn", rounds.loop_liveness_timeout_s()),
        _THRESHOLD_S,
    )

    claimed, _ = attempts.claim_attempts(pool, attempts.HOSTED_TURN, [agent_id], 600.0)
    assert claimed == []


async def test_a_slow_recovery_is_never_started_twice(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single flight is the loop's sequencing: while a recovery spans many
    intervals the loop is inside the round, and afterwards inside the cooldown."""
    agent_id = _make_hosted_running_agent(db_conn)
    _wedged_runner_redis(monkeypatch)
    release = asyncio.Event()
    recovered: list[int] = []

    async def slow(db_pool: object, _db: object, wedge: watchdog._HostedTurnWedge) -> None:
        recovered.append(wedge.agent_id)
        await release.wait()

    monkeypatch.setattr(watchdog, "_recover_hosted_turn", slow)
    loop_task = asyncio.create_task(
        watchdog.hosted_turn_recovery_loop(
            pool,
            Database.from_settings(),
            EventBus.from_settings(),
            LoopProgress("hosted_turn", rounds.loop_liveness_timeout_s()),
            0.01,
            _THRESHOLD_S,
        )
    )
    try:
        await asyncio.sleep(0.3)
        assert recovered == [agent_id]
        release.set()
        await asyncio.sleep(0.3)
        assert recovered == [agent_id]
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
