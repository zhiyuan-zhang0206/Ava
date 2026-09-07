"""Out-of-process liveness detection and recovery for hosted agent turns."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import NamedTuple, Protocol, TypeGuard, cast
from uuid import UUID

from psycopg_pool import ConnectionPool

import shared.db
import shared.redis_client
from shared import telemetry
from shared.hosted_db_wait import database_wait_matches

_log = logging.getLogger("services.delivery_watchdog.turn_liveness")

HOSTED_TURN_RECOVERY_COOLDOWN_S = 600.0


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
    for candidate in select_hosted_turn_liveness_candidates(pool, threshold_s):
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


def _queue_hosted_turn_recovery(pool: ConnectionPool, agent_id: int) -> int:
    """Create durable work so guarded resurrection retries survive restarts."""
    with pool.connection() as conn:
        return shared.db.insert_inbound_message(
            conn,
            agent_id,
            "Your previous hosted turn stopped making progress and was restarted "
            "by the delivery watchdog. Continue from the latest checkpoint.",
            source="system",
        )


async def _recover_hosted_turn(pool: ConnectionPool, wedge: _HostedTurnWedge) -> None:
    """Record evidence, force-terminate the hosted incarnation, then resurrect."""
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
        from ops.ops_lifecycle import resurrect_if_terminated, terminate_agent_op
        from ops.rpc_schemas import TerminateAgentRequest

        await terminate_agent_op(
            wedge.agent_id,
            TerminateAgentRequest(force=True, source="system"),
            pool,
        )
        trigger_id = await asyncio.to_thread(_queue_hosted_turn_recovery, pool, wedge.agent_id)
        status = await resurrect_if_terminated(
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
        # The durable recovery chat is picked up by the watchdog's existing
        # terminated-owner resurrection retry if the first resurrection loses
        # a race with hosted-force quiescence.
        _log.exception("[delivery] hosted turn recovery failed for agent %s", wedge.agent_id)


_last_hosted_turn_recovery_attempt: dict[int, float] = {}
_hosted_turn_recovery_tasks: dict[int, asyncio.Task[None]] = {}


def _maybe_spawn_hosted_turn_recoveries(
    pool: ConnectionPool,
    wedges: list[_HostedTurnWedge],
) -> None:
    """Start at most one recovery per agent in each ten-minute cooldown."""
    now = time.monotonic()
    for wedge in wedges:
        if wedge.agent_id in _hosted_turn_recovery_tasks:
            continue
        last_attempt = _last_hosted_turn_recovery_attempt.get(wedge.agent_id)
        if last_attempt is not None and now - last_attempt < HOSTED_TURN_RECOVERY_COOLDOWN_S:
            continue
        _last_hosted_turn_recovery_attempt[wedge.agent_id] = now
        task = asyncio.create_task(_recover_hosted_turn(pool, wedge))
        _hosted_turn_recovery_tasks[wedge.agent_id] = task

        def _discard(
            completed: asyncio.Task[None], *, completed_agent_id: int = wedge.agent_id
        ) -> None:
            if _hosted_turn_recovery_tasks.get(completed_agent_id) is completed:
                del _hosted_turn_recovery_tasks[completed_agent_id]

        task.add_done_callback(_discard)


async def scan_hosted_turn_liveness(pool: ConnectionPool, threshold_s: float) -> None:
    """Run one Redis-confirmed scan on the delivery watchdog's existing tick."""
    redis_client = cast(_RedisReader, shared.redis_client.get_async_redis())
    wedges = await _detect_hosted_turn_wedges(pool, threshold_s, redis_client)
    _maybe_spawn_hosted_turn_recoveries(pool, wedges)
