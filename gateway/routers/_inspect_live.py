"""Fresh database projections for the agent inspector endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, NamedTuple, cast

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from gateway.schemas import HeartbeatInfo, HeartbeatLastPause
from services.heartbeat import JITTER_SPAN_S, STALE_PENDING_S
from shared.agent_observation import AgentObservation, observation
from shared.agent_snapshot import OpenNotice
from shared.agents import AgentStatus
from shared.config import settings
from shared.db import NOTICE_FYI_TTL_DAYS


class InspectDbRows(NamedTuple):
    """Live agents_meta fields that must never ride the aggregate TTL."""

    # True only for a FRESH pending inbound (created within STALE_PENDING_S):
    # a stale one no longer counts as "about to wake" — the daemon checks in on
    # it anyway, so the panel projects next_at instead of heartbeat_pending.

    machine: str
    status: Any
    last_active_at: Any
    spawned_at: Any
    started_at: Any
    paused_until: Any
    pending_inbound: bool
    config_overlay: dict[str, Any]
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: Any
    observation: AgentObservation


def db_rows_blocking(pool: ConnectionPool[Any], agent_id: int) -> InspectDbRows:
    """Read agents_meta and the fresh pending-inbound flag in one DB borrow."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, machine, status, last_active_at, "
            "       spawned_at, started_at, "
            "       CASE WHEN heartbeat_paused_until > now() THEN heartbeat_paused_until END, "
            "       liveness_state, last_probe_at, lease_expires_at, "
            "       (SELECT mp.last_probe_at FROM machine_probe mp "
            "        WHERE mp.machine_name = agents_meta.machine) "
            "FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending' "
            "AND created_at >= now() - make_interval(secs => %s))",
            (agent_id, STALE_PENDING_S),
        )
        pending_row = cur.fetchone()
        assert pending_row is not None  # noqa: S101 — EXISTS always returns one row
        return InspectDbRows(
            machine=row[1],
            status=row[2],
            last_active_at=row[3],
            spawned_at=row[4],
            started_at=row[5],
            paused_until=row[6],
            pending_inbound=bool(pending_row[0]),
            config_overlay=row[0] if row[0] is not None else {},
            liveness_state=cast(
                Literal["online", "offline", "unknown"],
                row[7] if row[7] is not None else "unknown",
            ),
            last_probe_at=row[8],
            observation=observation(row[10], row[9]),
        )


def notice_blocking(pool: ConnectionPool[Any], agent_id: int) -> OpenNotice | None:
    """Read the agent's single unexpired open notice, if one exists."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, content, priority, require_response, blocking, created_at "
            "FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL "
            "AND (require_response OR created_at > now() - make_interval(days => %s)) "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, NOTICE_FYI_TTL_DAYS),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return OpenNotice(
        id=row[0],
        title=row[1],
        content=row[2],
        priority=row[3],
        require_response=row[4],
        blocking=row[5],
        created_at=row[6],
    )


def project_heartbeat(
    status: str,
    last_active_at: datetime,
    paused_until: datetime | None,
    *,
    agent_id: int,
    pending_inbound: bool,
    last_pause: HeartbeatLastPause | None,
) -> HeartbeatInfo:
    """Idle-heartbeat state for one agent — the projected next check-in (or the
    active pause / already-queued wake) plus the most recent pause from history.

    `paused_until` is the agents_meta column already narrowed by the caller to a
    *future* value (a NULL / past pause arrives here as None = not paused). The
    display states are mutually exclusive:

    - idle-family (idling / restarting), paused → `paused_until`.
    - idle-family, not paused, a wake already queued → `heartbeat_pending`.
    - idle-family, not paused, nothing queued → `next_at` (the daemon's projected
      check-in due time — idle clock plus the per-agent jitter; the daemon
      dispatches at its first poll tick at/after that, at most one 15s dispatch
      step later. The frontend renders an overdue one as "due").
    - running or terminated → all off (never checked in on).

    `heartbeat_pending` mirrors the daemon's `NOT EXISTS (pending inbound)` guard
    in full — including its 900s freshness window (`STALE_PENDING_S`): only a
    *fresh* pending inbound (a heartbeat check-in already sent but not yet
    processed, or a chat about to wake it) suppresses the daemon's next check-in.
    A pending inbound older than that window no longer counts as "about to wake" —
    the daemon checks in on the agent anyway. Surfacing this state —
    instead of projecting `next_at` off a stale clock — is what keeps a stuck
    agent (an unconsumed check-in it never woke to process) from rendering a
    nonsensical past "next heartbeat" time. When no inbound is pending, any
    earlier check-in was already consumed (its turn bumped `last_active_at` past
    it), so `last_active_at` alone is the correct projection basis — no
    `heartbeat_nudged`-event floor is needed.

    `last_pause` is the cached event-history aggregate. Every other input is
    from the request's fresh agents_meta read, so a status/heartbeat change is
    visible immediately even while the historical Loki fan-out rides its TTL.
    """
    # The daemon projects the next check-in from last_active_at + idle_threshold_s
    # (plus per-agent jitter). Use idle_threshold_s, not heartbeat_interval_seconds
    # (the poll cadence), so the inspector matches the daemon's contract.
    # heartbeat_interval_seconds is kept for the return value — the frontend badge
    # ("every 5m") uses it as the configured check-in cadence.
    idle_threshold_s = int(settings.daemon.heartbeat_idle_threshold_seconds)
    interval_s = int(settings.daemon.heartbeat_interval_seconds)

    next_at: datetime | None = None
    active_pause: datetime | None = None
    heartbeat_pending = False
    if status in (AgentStatus.IDLING, AgentStatus.RESTARTING):
        if paused_until is not None:
            active_pause = paused_until
        # Mirror the daemon's `NOT EXISTS (pending inbound)` guard: with a
        # wake already queued, no check-in is scheduled (heartbeat_pending);
        # with nothing queued, project from the idle clock.
        # `pending_inbound` is computed by the caller on the same DB
        # connection (the inspector's agents_meta read).
        elif pending_inbound:
            heartbeat_pending = True
        else:
            # Overdue (daemon skips restarting agents) → frontend shows "due".
            # Match the daemon's due-time exactly: idle clock + per-agent jitter
            # (id mod JITTER_SPAN_S) — the actual dispatch happens at the first
            # daemon tick at/after this, so next_at never overstates it.
            # JITTER_SPAN_S is int-typed (whole seconds) so this matches the
            # daemon's `NULLIF(span, 0)::int` cast exactly; the `0` guard mirrors
            # the daemon's disabled-jitter collapse (mod never divides by zero).
            jitter_s = agent_id % JITTER_SPAN_S if JITTER_SPAN_S else 0
            next_at = last_active_at + timedelta(seconds=idle_threshold_s + jitter_s)
    return HeartbeatInfo(
        interval_s=interval_s,
        next_at=next_at,
        paused_until=active_pause,
        heartbeat_pending=heartbeat_pending,
        last_pause=last_pause,
    )
