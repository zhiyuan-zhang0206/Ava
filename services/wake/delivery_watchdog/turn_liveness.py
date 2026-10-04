"""Out-of-process liveness detection and recovery for hosted agent turns.

Runs as one resident sequential loop: each round selects the hosted agents
whose DB clock is stale, confirms them against the host's Redis beat, and
recovers the confirmed wedges (at most one recovery per agent per persisted
ten-minute cooldown, a few at a time).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
from typing import NamedTuple, Protocol, TypeGuard, cast
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.incarnation.lifecycle_acceptance import HOSTED_TURN_RECOVERY_MARKER
from base.agents.observation.db_wait import database_wait_matches
from base.config.service_read import current_field_values
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.wake.delivery_watchdog import attempts, rounds

_log = logging.getLogger("services.wake.delivery_watchdog.turn_liveness")

HOSTED_TURN_RECOVERY_COOLDOWN_S = 600.0
_HOSTED_TURN_RECOVERY_MAX_CONCURRENCY = 4
HOSTED_TURN_RECOVERY_WAKE_TEXT = (
    "Your previous hosted turn stopped making progress and was restarted "
    "by the delivery watchdog. Continue from the latest checkpoint."
)


class _RedisReader(Protocol):
    async def get(self, key: str) -> str | bytes | None: ...


class _HostedTurnCandidate(NamedTuple):
    agent_id: int
    machine: str
    db_age_s: float
    generation: UUID | None = None
    owner: UUID | None = None


class _HostedTurnWedge(NamedTuple):
    agent_id: int
    machine: str
    age_s: float
    last_marks: tuple[float, ...]
    heartbeat_missing: bool


class _ProgressSnapshot(NamedTuple):
    age_s: float
    last_marks: tuple[float, ...]
    db_wait: object = None


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def select_hosted_turn_liveness_candidates(
    pool: ConnectionPool,
    threshold_s: float,
) -> list[_HostedTurnCandidate]:
    """Stale DB clocks for exactly the hosted agents currently marked running."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, machine, EXTRACT(EPOCH FROM (now() - last_active_at)), "
            "runtime_generation, runtime_owner "
            "FROM agents_meta WHERE runtime_kind='hosted' AND status='running' "
            "AND last_active_at < now() - make_interval(secs => %s) ORDER BY id",
            (threshold_s,),
        )
        return [_HostedTurnCandidate(r[0], r[1], float(r[2]), r[3], r[4]) for r in cur.fetchall()]


def _parse_progress_snapshot(raw: str | bytes, agent_id: int) -> _ProgressSnapshot | None:
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("host turn-progress heartbeat must be an object")
    payload = cast(dict[str, object], parsed)
    snapshot = payload.get(str(agent_id))
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise TypeError("agent turn-progress snapshot must be an object")
    snapshot = cast(dict[str, object], snapshot)
    age = snapshot["age_s"]
    raw_marks = snapshot["last_marks"]
    if not _is_finite_number(age) or not isinstance(raw_marks, list):
        raise ValueError("agent turn-progress snapshot has invalid clock values")
    mark_values = cast(list[object], raw_marks)
    if len(mark_values) > 3:
        raise ValueError("agent turn-progress snapshot has invalid clock values")
    marks: list[float] = []
    for mark in mark_values:
        if not _is_finite_number(mark):
            raise ValueError("agent turn-progress snapshot has invalid clock values")
        marks.append(float(mark))
    return _ProgressSnapshot(float(age), tuple(marks), snapshot.get("db_wait"))


async def _detect_hosted_turn_wedges(
    pool: ConnectionPool,
    threshold_s: float,
    redis_client: _RedisReader,
) -> list[_HostedTurnWedge]:
    """Confirm stale DB candidates against the host's independent Redis beat."""
    wedges: list[_HostedTurnWedge] = []
    for candidate in await asyncio.to_thread(
        select_hosted_turn_liveness_candidates, pool, threshold_s
    ):
        try:
            raw = await redis_client.get(f"host_turn_progress:{candidate.machine}")
            if raw is None:
                wedges.append(
                    _HostedTurnWedge(
                        agent_id=candidate.agent_id,
                        machine=candidate.machine,
                        age_s=candidate.db_age_s,
                        last_marks=(),
                        heartbeat_missing=True,
                    )
                )
                continue
            snapshot = _parse_progress_snapshot(raw, candidate.agent_id)
            if snapshot is not None and database_wait_matches(
                snapshot.db_wait, candidate.generation, candidate.owner
            ):
                continue
            if snapshot is not None and snapshot.age_s >= threshold_s:
                wedges.append(
                    _HostedTurnWedge(
                        agent_id=candidate.agent_id,
                        machine=candidate.machine,
                        age_s=snapshot.age_s,
                        last_marks=snapshot.last_marks,
                        heartbeat_missing=False,
                    )
                )
        except Exception:
            # Unreadable evidence cannot license a destructive recovery. A
            # successful GET returning None is handled above as an expired beat.
            _log.debug(
                "[delivery] hosted turn-progress read failed for agent %s on %s",
                candidate.agent_id,
                candidate.machine,
                exc_info=True,
            )
    return wedges


def _recovery_trigger(pool: ConnectionPool, agent_id: int) -> int:
    """The pending recovery wake the force terminate committed for `agent_id`.

    The wake is part of the termination's own transaction
    (`terminate_agent_op(recovery_wake=...)`), so it is already durable: the
    direct resurrection below and the watchdog's terminated-owner retry both
    work from it, and neither can lose it to a restart. It carries the
    `HOSTED_TURN_RECOVERY_MARKER` payload: a system-source message that is this
    recovery's own wake-up call, so the notice predicate lets it through both
    resurrection channels — unlike a plain system notification, which never
    resurrects (task #3687 review, Ava #3242)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT m.id FROM inbound_messages m JOIN agents_meta a ON a.id = m.agent_id "
            "WHERE m.agent_id = %s AND m.kind = 'chat' AND m.status = 'pending' "
            "AND m.id > COALESCE(a.last_force_terminate_inbound_id, 0) "
            "AND m.payload @> %s ORDER BY m.id LIMIT 1",
            (agent_id, Jsonb({HOSTED_TURN_RECOVERY_MARKER: True})),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"force-terminated agent {agent_id} has no pending recovery wake")
    return int(row[0])


async def _recover_hosted_turn(
    pool: ConnectionPool, db: Database, bus: EventBus, wedge: _HostedTurnWedge
) -> None:
    """Record evidence, force-terminate the hosted incarnation together with its
    recovery wake, then resurrect."""
    _log.error(
        "[delivery] host_turn_wedged_recovery agent_id=%s age_s=%.1f "
        "last_marks=%s machine=%s heartbeat_missing=%s",
        wedge.agent_id,
        wedge.age_s,
        list(wedge.last_marks),
        wedge.machine,
        wedge.heartbeat_missing,
    )
    try:
        telemetry.emit(
            "telemetry",
            "host_turn_stall_detected",
            level="error",
            agent_id=wedge.agent_id,
            source="system",
            attributes={
                "age_s": round(wedge.age_s, 1),
                "last_marks": list(wedge.last_marks),
                "machine": wedge.machine,
                "heartbeat_missing": wedge.heartbeat_missing,
                "detector": "delivery_watchdog",
            },
        )
    except Exception:
        _log.exception(
            "[delivery] hosted turn wedge event emit failed for agent %s", wedge.agent_id
        )

    try:
        from ops.lifecycle import resurrect_if_terminated, terminate_agent_op
        from ops.rpc_schemas import TerminateAgentRequest

        await terminate_agent_op(
            db,
            bus,
            wedge.agent_id,
            TerminateAgentRequest(force=True, source="system"),
            pool,
            recovery_wake=HOSTED_TURN_RECOVERY_WAKE_TEXT,
        )
        trigger_id = await asyncio.to_thread(_recovery_trigger, pool, wedge.agent_id)
        status = await resurrect_if_terminated(
            db,
            bus,
            wedge.agent_id,
            trigger_inbound_id=trigger_id,
            trigger_inbound_kind="chat",
        )
        _log.info(
            "[delivery] hosted turn recovery for agent %s queued trigger %s -> status %s",
            wedge.agent_id,
            trigger_id,
            status,
        )
    except Exception:
        # Once the terminate has committed, so has its recovery chat: the
        # watchdog's existing terminated-owner resurrection retry resumes the
        # agent from it if this attempt dies or loses a race with hosted-force
        # quiescence. A failure before the commit leaves the agent running and
        # wedged, so a later scan finds it again.
        _log.exception("[delivery] hosted turn recovery failed for agent %s", wedge.agent_id)


def hosted_turn_threshold_seconds() -> float:
    """Read the runner-owned threshold from the gateway's current `.env` view.

    The alias belongs to the agent-runner config projection and is removed from
    the gateway process environment, so the gateway-owned `.env` snapshot is the
    authority an operator override is preserved at."""
    return float(current_field_values()["wedged_agent_inbound_age_seconds"])


async def _recover_within_deadline(
    pool: ConnectionPool, db: Database, bus: EventBus, wedge: _HostedTurnWedge
) -> None:
    """One recovery under the RPC deadline. A timeout between the terminate's
    commit and the resurrect leaves the committed recovery wake for the
    terminated-owner retry, exactly as any other failure there does."""
    try:
        try:
            async with asyncio.timeout(rounds.rpc_deadline_s()):
                await _recover_hosted_turn(pool, db, bus, wedge)
        except TimeoutError:
            _log.error(
                "[delivery] hosted turn recovery for agent %s exceeded %.0fs",
                wedge.agent_id,
                rounds.rpc_deadline_s(),
            )
    finally:
        await asyncio.to_thread(attempts.finish_attempt, pool, attempts.HOSTED_TURN, wedge.agent_id)


async def hosted_turn_recovery_round(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress, threshold_s: float
) -> None:
    """One Redis-confirmed scan; recover the wedges whose per-agent cooldown
    has elapsed, then return."""
    redis_client = cast(_RedisReader, bus.async_redis())
    wedges = await _detect_hosted_turn_wedges(pool, threshold_s, redis_client)
    claimed, _deferred = await asyncio.to_thread(
        attempts.claim_attempts,
        pool,
        attempts.HOSTED_TURN,
        [wedge.agent_id for wedge in wedges],
        HOSTED_TURN_RECOVERY_COOLDOWN_S,
    )
    by_agent = {wedge.agent_id: wedge for wedge in wedges}
    await round_loop.fan_out(
        [functools.partial(_recover_within_deadline, pool, db, bus, by_agent[a]) for a in claimed],
        concurrency=_HOSTED_TURN_RECOVERY_MAX_CONCURRENCY,
        progress=progress,
    )


async def hosted_turn_recovery_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    interval_s: float,
    threshold_s: float,
) -> None:
    """The hosted-turn liveness recovery as a resident sequential loop."""

    async def one_round() -> None:
        await hosted_turn_recovery_round(pool, db, bus, progress, threshold_s)

    await round_loop.run_rounds("hosted-turn recovery", progress, interval_s, one_round)
