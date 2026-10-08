"""DB waits survive automatic stale scans, but never override explicit force."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.observation import db_wait
from base.agents.observation.db_wait import DatabaseWaits
from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, insert_inbound_message
from base.deploy.maintenance import cohort, pause_owner
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_async_redis
from base.native_process.turn_identity import bind_turn_identity
from services.agent_runner.agent_host import daemon, db_recovery
from services.agent_runner.agent_host.dispatcher import (
    InboundWakeDispatcher,
    PendingInboundWake,
    TurnScheduler,
)
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.recovery.test_hosted_db_recovery import _admit, _graph
from services.wake.delivery_watchdog import turn_liveness
from services.wake.delivery_watchdog.tests.test_delivery_watchdog_turn_liveness import FakeRedis


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(db_recovery, "_PROBE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(db_recovery, "_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(db_recovery, "_MAX_BACKOFF_SECONDS", 0.02)


def _mark_agent_stale_with_pending_cause(
    db_conn: psycopg.Connection,
    agent: int,
    incarnation: Any,
    *,
    held: bool,
    event_bus: EventBus,
    database: Database,
) -> None:
    """Make the agent look 100s stale, held by maintenance or by an old pending message."""
    if held:
        acquired = datetime.now(UTC)
        pause_owner.begin_maintenance("waiting", acquired)
        cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=incarnation.owner,
            holder="waiting",
            acquired_at=acquired,
        )
    else:
        message = insert_inbound_message(
            db_conn, agent, "pending", "user", bus=event_bus, database=database
        )
        db_conn.execute(
            "UPDATE inbound_messages SET created_at=now()-interval '100s' WHERE id=%s", (message,)
        )
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '100s' WHERE id=%s", (agent,)
    )
    db_conn.commit()


def _recovery_run(
    waits: DatabaseWaits,
    control: AsyncConnectionPool,
    graph: Any,
    saver: Any,
    incarnation: Any,
    recovered: asyncio.Event,
    cancelled: asyncio.Event,
) -> Callable[[int], Awaitable[None]]:
    """A turn body that waits in real DB recovery, then parks until cancelled."""
    agent = incarnation.agent_id

    async def run(_agent: int) -> None:
        try:
            with bind_turn_identity(agent, incarnation=incarnation):
                await db_recovery.recover_database(
                    pool=control,
                    graph=graph,
                    checkpointer=saver,
                    incarnation=incarnation,
                    database_waits=waits,
                    peek_lock=asyncio.Lock(),
                )
            recovered.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    return run


async def _wait_until_database_wait_is_visible(waits: DatabaseWaits, agent: int) -> None:
    async with asyncio.timeout(2):
        while waits.snapshot(agent) is None:
            await asyncio.sleep(0.001)


async def _assert_automatic_scan_spares_the_database_wait(
    dispatcher: InboundWakeDispatcher,
    scheduler: TurnScheduler,
    agent: int,
    cancelled: asyncio.Event,
    clock: TurnProgress,
) -> None:
    before = clock.snapshot(agent)
    await dispatcher.scan_once()
    assert agent in scheduler.active_agents
    assert not cancelled.is_set()
    assert clock.snapshot(agent)["last_marks"] == before["last_marks"]  # type: ignore[index]


async def _assert_explicit_force_cancels_the_database_wait(
    waits: DatabaseWaits, scheduler: TurnScheduler, agent: int, cancelled: asyncio.Event
) -> None:
    async def validate(_agent: int, _command: int) -> bool:
        return True

    assert await scheduler.cancel_exact_force(agent, 1, validate)
    assert cancelled.is_set()
    assert waits.snapshot(agent) is None


async def _assert_stale_handling_resumes_after_recovery(
    waits: DatabaseWaits,
    dispatcher: InboundWakeDispatcher,
    recovered: asyncio.Event,
    cancelled: asyncio.Event,
    agent: int,
) -> None:
    await asyncio.wait_for(recovered.wait(), 2)
    assert waits.snapshot(agent) is not None
    await dispatcher.scan_once()
    assert not cancelled.is_set(), "finite success handoff also protects the next DB stage"
    waits._waiting[agent].deadline = time.monotonic() - 1
    assert waits.snapshot(agent) is None
    await dispatcher.scan_once()
    assert cancelled.is_set(), "normal stale handling must resume after DB recovery"


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_real_db_wait_survives_both_stale_paths_and_clears_afterward(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    held: bool,
    force: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: Any) -> dict[str, Any]:
        raise AssertionError("a recovery wait does not invoke the graph")

    graph, saver = await _graph(aops_pool, agent, never)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    host._owner = incarnation.owner
    _mark_agent_stale_with_pending_cause(
        db_conn, agent, incarnation, held=held, event_bus=event_bus, database=database
    )
    recovered, cancelled = asyncio.Event(), asyncio.Event()
    waits = DatabaseWaits()
    clock = host.turn_progress
    clock._marks[agent] = [time.monotonic() - 100]
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs={"autocommit": True}
    ) as control:
        scheduler = TurnScheduler(
            _recovery_run(waits, control, graph, saver, incarnation, recovered, cancelled)
        )
        dispatcher = InboundWakeDispatcher(
            scheduler=scheduler,
            bus=EventBus.from_settings(),
            pending_scan=host.pending_inbound_wakes,
            database_waits=waits,
            turn_progress=clock,
            stale_after_s=1.0,
        )
        try:
            async with control.connection():
                scheduler.wake(agent)
                await _wait_until_database_wait_is_visible(waits, agent)
                await _assert_automatic_scan_spares_the_database_wait(
                    dispatcher, scheduler, agent, cancelled, clock
                )
                if force:
                    await _assert_explicit_force_cancels_the_database_wait(
                        waits, scheduler, agent, cancelled
                    )
            if not force:
                await _assert_stale_handling_resumes_after_recovery(
                    waits, dispatcher, recovered, cancelled, agent
                )
        finally:
            await scheduler.aclose()
            clock._marks.pop(agent, None)
        assert waits.snapshot(agent) is None


@pytest.mark.parametrize(
    "proof_kind",
    [
        "fresh",
        "clock_ahead",
        "clock_behind",
        "old_owner",
        "old_generation",
        "expired",
        "future",
        "oversized",
        "invalid",
        "worst_beat_phase",
    ],
)
async def test_gateway_exemption_requires_current_db_identity_and_finite_fresh_proof(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    proof_kind: str,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '100s' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    now = 1000.0
    ttl = db_wait.DB_WAIT_PROOF_TTL_SECONDS
    monkeypatch.setattr(db_wait.time, "time", lambda: now)
    proof: dict[str, object] = {
        "generation": str(incarnation.generation),
        "owner": str(incarnation.owner),
        "observed_at": now,
        "expires_at": now + ttl,
    }
    if proof_kind == "old_owner":
        proof["owner"] = str(uuid4())
    elif proof_kind == "old_generation":
        proof["generation"] = str(uuid4())
    elif proof_kind == "expired":
        proof.update(observed_at=now - ttl, expires_at=now)
    elif proof_kind == "future":
        proof.update(observed_at=now + 6, expires_at=now + ttl)
    elif proof_kind == "oversized":
        proof["expires_at"] = now + ttl + 1
    elif proof_kind == "invalid":
        proof["expires_at"] = float("nan")
    elif proof_kind in ("clock_ahead", "clock_behind"):
        skew = 0.5 if proof_kind == "clock_ahead" else -0.5
        proof.update(observed_at=now + skew, expires_at=now + ttl + skew)
    elif proof_kind == "worst_beat_phase":
        proof.update(observed_at=now - 88, expires_at=now + 12)
    redis = FakeRedis(
        {
            f"host_turn_progress:{machine_name()}": json.dumps(
                {str(agent): {"age_s": 100.0, "last_marks": [1.0], "db_wait": proof}}
            )
        }
    )
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1
    ) as pool:
        wedges = await turn_liveness._detect_hosted_turn_wedges(pool, 1.0, redis)
    exempt = proof_kind in {"fresh", "clock_ahead", "clock_behind", "worst_beat_phase"}
    assert [w.agent_id for w in wedges] == ([] if exempt else [agent])


async def test_heartbeat_preserves_progress_and_cannot_extend_wait_proof(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    clock = TurnProgress()
    clock._marks[agent] = [time.monotonic() - 100]
    writes: list[dict[str, Any]] = []

    class CaptureRedis:
        async def set(self, _key: str, value: str, *, ex: int) -> None:
            writes.append(json.loads(value))

    patch_async_redis(monkeypatch, CaptureRedis)
    waits = DatabaseWaits()
    try:
        with waits.wait(incarnation) as waiting:
            waiting.renew()
            await daemon._publish_turn_progress_heartbeat(
                EventBus.from_settings(), machine_name(), {agent}, waits, clock
            )
            await daemon._publish_turn_progress_heartbeat(
                EventBus.from_settings(), machine_name(), {agent}, waits, clock
            )
            assert writes[0][str(agent)]["db_wait"] == writes[1][str(agent)]["db_wait"]
            assert writes[0][str(agent)]["age_s"] >= 100
            assert writes[0][str(agent)]["last_marks"] == clock._marks[agent]
            waiting.deadline = time.monotonic() - 1
            assert waits.snapshot(agent) is None
            await daemon._publish_turn_progress_heartbeat(
                EventBus.from_settings(), machine_name(), {agent}, waits, clock
            )
            assert "db_wait" not in writes[-1][str(agent)]
        assert waits.snapshot(agent) is None
    finally:
        clock._marks.pop(agent, None)


async def test_success_handoff_clears_on_actual_node_progress(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    clock = TurnProgress()
    clock._marks[agent] = [time.monotonic() - 100]
    writes: list[dict[str, Any]] = []

    class CaptureRedis:
        async def set(self, _key: str, value: str, *, ex: int) -> None:
            writes.append(json.loads(value))

    patch_async_redis(monkeypatch, CaptureRedis)
    waits = DatabaseWaits()
    try:
        with waits.wait(incarnation) as waiting:
            waiting.renew()
            waiting.complete()
        await daemon._publish_turn_progress_heartbeat(
            EventBus.from_settings(), machine_name(), {agent}, waits, clock
        )
        assert "db_wait" in writes[-1][str(agent)]
        clock.mark(agent)
        await daemon._publish_turn_progress_heartbeat(
            EventBus.from_settings(), machine_name(), {agent}, waits, clock
        )
        assert "db_wait" not in writes[-1][str(agent)]
        assert waits.snapshot(agent) is None
    finally:
        clock._marks.pop(agent, None)


@pytest.mark.parametrize("pending_stale", [False, True])
async def test_pending_scan_gives_inactive_agent_a_turn_before_using_its_old_clock(
    pending_stale: bool,
) -> None:
    agent = 876501
    clock = TurnProgress()
    started = [asyncio.Event(), asyncio.Event()]
    calls, cancelled = 0, 0
    scans = 0

    async def run(_agent: int) -> None:
        nonlocal calls, cancelled
        clock.reset(agent)
        started[min(calls, 1)].set()
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    async def pending(_age: float) -> list[PendingInboundWake]:
        nonlocal scans
        scans += 1
        return [PendingInboundWake(agent, pending_stale)] if scans == 1 else []

    scheduler = TurnScheduler(run)
    dispatcher = InboundWakeDispatcher(
        bus=EventBus.from_settings(),
        scheduler=scheduler,
        pending_scan=pending,
        turn_progress=clock,
        stale_after_s=1.0,
    )
    clock._marks[agent] = [time.monotonic() - 100]
    try:
        assert agent not in scheduler.active_agents
        await dispatcher.scan_once()
        assert agent in scheduler.active_agents
        assert cancelled == 0
        await asyncio.wait_for(started[0].wait(), 1)
        assert calls == 1
        # The exemption lasts for one scan only. A genuinely stalled task on
        # the next scan must still cancel and admit a fresh runnable task.
        clock._marks[agent] = [time.monotonic() - 100]
        await dispatcher.scan_once()
        assert cancelled == 1
        await asyncio.wait_for(started[1].wait(), 1)
        assert calls == 2
        assert not scheduler.restart_required
    finally:
        await scheduler.aclose()
        clock._marks.pop(agent, None)
    assert not scheduler.active_agents
