"""Machine pause/resume endpoints — `POST /api/cluster/machines/{name}/pause`
and `.../resume` (Task #1283: temporarily pull a machine out of the cluster,
e.g. the operator is away for a week and disconnects it; the cluster then
shows only its active members, no offline alert fires for the expected
absence, rollouts and spawns skip it, and `ava cluster resume` brings it
back).

Split out of gateway/routers/cluster.py at the 800-line ceiling (same pattern
as `_roster_rows.py`): the pause contract is the three-step operator act
below, the roster/staging/delete endpoints stay in cluster.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool

from gateway.routers import agents_forward
from gateway.schemas import (
    MachinePauseRequest,
    MachinePauseResponse,
    MachineResumeResponse,
)
from ops.ops_lifecycle import _force_mark_terminated
from shared import machines
from shared.machine import machine_name
from shared.task_notes import task_note_line

router = APIRouter()

# The drain owner every open/in_progress task of a paused machine's agents is
# reassigned to. #405 is the Ava P0 lead — the role that redistributes work when
# an owner disappears (2026-08-14 Task #1283 ruling: drain reassigns, with a
# note on each task; the reminder daemon surfaces the reassignment to the new
# owner). A constant, not a flag: the pause is an operator act on the gateway,
# and the CLI surface stays one verb.
_MACHINE_PAUSE_DRAIN_OWNER = 405


def _read_machine_row(pool: ConnectionPool, name: str) -> tuple[bool, datetime | None, str | None]:
    """Sync row read — via to_thread: does the machine exist, and what is its
    pause state?"""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT paused_at, pause_reason FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        return False, None, None
    return True, row[0], row[1]


def _drain_tasks_blocking(pool: ConnectionPool, name: str) -> int:
    """Reassign every open/in_progress task owned by a LIVE agent on `name`
    to the drain owner (#405), appending a note on each so the new owner knows
    why it landed on their board.

    The note goes through `shared.task_notes.task_note_line`, the same builder
    the SDK task registry uses, so the two writers into one `results` column
    cannot drift apart on format or timezone; the write resets the reminder
    clock like a normal update would. Agents already terminated before the
    pause own nothing that this pause needs to rescue (their tasks are another
    cleanup's business).
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, a.id FROM agent_tasks t "
                "JOIN agents_meta a ON t.owner = a.id "
                "WHERE a.machine = %s AND a.status != 'terminated' "
                "AND t.status IN ('open', 'in_progress')",
                (name,),
            )
            rows = cur.fetchall()
        for task_id, owner_id in rows:
            note = task_note_line(
                f"machine {name} paused \u2014 owner agent #{owner_id} "
                f"terminated; task reassigned to drain owner #{_MACHINE_PAUSE_DRAIN_OWNER} "
                f"(see `ava cluster resume {name}`)"
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_tasks SET owner = %s, updated_at = now(), "
                    "results = COALESCE(results, '') || %s "
                    "WHERE id = %s",
                    (_MACHINE_PAUSE_DRAIN_OWNER, "\n" + note, task_id),
                )
        conn.commit()
    return len(rows)


def _list_agent_rows_for_pause_blocking(pool: ConnectionPool, name: str) -> list[tuple[int, bool]]:
    """Lock every agent row on `name`, returning ``(id, was_live)``.

    The pause latch is already committed before this sweep. Locking even rows
    that look terminated is deliberate: a pre-latch resurrect may hold an
    uncommitted terminated -> idling transition. ``FOR UPDATE`` waits for
    it and rechecks the current row, so the subsequent force fence is ordered
    after that resurrection. Already-terminated rows are included too: pause
    is an explicit kill intent and must fence their older pending work.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM agents_meta WHERE machine = %s FOR UPDATE",
            (name,),
        )
        return [(r[0], r[1] != "terminated") for r in cur.fetchall()]


def _force_mark_terminated_blocking(pool: ConnectionPool, agent_id: int) -> None:
    """Force-mark one agent row terminated in the shared DB — the fallback
    when the machine's ops server could not take the force terminate (the
    machine is already unreachable). Reuses the lifecycle primitive so the
    fallback writes the same terminate inbound fence under the agent row lock;
    the process is on the unreachable machine, so there is no local OS session
    to kill."""
    _force_mark_terminated(agent_id, pool, source="machine-pause")


def _resolve_machine_alerts_blocking(pool: ConnectionPool, name: str) -> None:
    """Resolve every open "machine offline" alert for `name` — a paused
    machine's absence is expected, not an incident, so an alert that was
    already firing when the pause ran must not stay open (and spam IM) for
    the whole pause window. Mirrors the liveness pass's recovery edge
    (`services.heartbeat.liveness._machine_alert_edges`) so the fingerprint
    and the resolve semantics match the firing edge exactly."""
    from services.heartbeat.liveness import _machine_alert_labels
    from shared.alerts import AlertKey, fingerprint, stamp_notified, upsert_alert

    labels = _machine_alert_labels(name)
    fp = fingerprint(labels)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT starts_at FROM alerts WHERE fingerprint = %s AND status = 'unresolved' "
                "ORDER BY starts_at DESC",
                (fp,),
            )
            open_rows = [r[0] for r in cur.fetchall()]
        keys: list[AlertKey] = []
        for starts_at in open_rows:
            alert = {
                "status": "resolved",
                "labels": labels,
                "annotations": {"summary": f"machine {name} paused (expected absence)"},
                "starts_at": starts_at.isoformat(),
                "ends_at": datetime.now(UTC).isoformat(),
                "fingerprint": fp,
            }
            key, _did_insert, should_notify, _row = upsert_alert(
                conn, alert, source="machine-pause"
            )
            if should_notify:
                keys.append(key)
        if keys:
            stamp_notified(conn, keys)
        conn.commit()


@router.post("/api/cluster/machines/{name}/pause", response_model=MachinePauseResponse)
async def pause_cluster_machine(
    name: str, req: MachinePauseRequest, request: Request
) -> MachinePauseResponse:
    """Temporarily pull a machine out of the cluster (`ava cluster pause`).

    The operator act starts with an admission fence, then drains and sweeps:

    1. **Latch** — set `paused_at`/`pause_reason` first. Every resurrection
       transaction (explicit, pending-work, controller, and retry) locks and
       checks this latch before its agent row, so stale resurrection cannot
       escape the sweep.
    2. **Drain** — every open/in_progress task owned by a live agent on this
       machine is reassigned to the drain owner (#405) with a note; the
       reminder daemon then surfaces it.
    3. **Final sweep** — every agent row on the machine, including an already
       terminated row, receives a newer force-terminate intent fence. A
       pre-latch resurrection holding the agent row lock completes first, then
       this sweep terminates its exact session; older pending work on a dead
       row cannot reverse the pause either. Other existing-agent re-entry paths
       (respawn/swap/revive) keep their established admission rules and are
       caught when they hold or update an existing row before the final sweep.

    The live rows are
       force-terminated via the machine's own ops server (kill the process +
       mark terminated in one op, source="machine-pause"): the machine is
       about to be disconnected, so waiting for graceful turns is not worth
       the non-determinism of an agent that may never claim. An agent whose
       ops server is unreachable (the machine is already down) is force-marked
       terminated in the shared DB — its process is on the departing host
       anyway.
    From the latch onward the machine vanishes from the roster / cluster panel /
    agents' list_machines, `list_agent_runners()` drops it (no probe, no offline
    alert, rollout skips it) and ordinary spawns targeting it are refused (409).
    The separate transaction race between the pause latch and creation of a
    brand-new agent row is outside this resurrection boundary.

    Open "machine offline" alerts for the machine are resolved as part of the
    latch step — an expected absence is not an incident. Idempotent: pausing
    an already-paused machine re-runs the (now-empty) drain/terminate and
    returns the existing latch.

    Refuses (400) to pause the gateway's own machine — the gateway host must
    stay a cluster member for the cluster to answer anything.
    """
    if name == machine_name():
        raise HTTPException(
            status_code=400,
            detail=f"refusing to pause this gateway's own machine ({name!r}); the "
            "cluster needs its gateway host online. Pause agent-runner machines only.",
        )
    pool = request.app.state.db_pool
    exists, paused_at, pause_reason = await asyncio.to_thread(_read_machine_row, pool, name)
    if not exists:
        raise HTTPException(status_code=404, detail=f"no machine named {name!r}")

    if paused_at is None:
        machines.pause(name, reason=req.reason)
        exists, paused_at, pause_reason = await asyncio.to_thread(_read_machine_row, pool, name)

    reassigned = await asyncio.to_thread(_drain_tasks_blocking, pool, name)

    agent_rows = await asyncio.to_thread(_list_agent_rows_for_pause_blocking, pool, name)

    terminated = force_marked = 0
    for agent_id, was_live in agent_rows:
        # The same lifecycle path the frontend/SDK terminate uses — the target
        # machine's ops server runs post_agent_terminate in-process. A machine
        # that is already unreachable raises; the agent's process is on the
        # departing host anyway, so force-mark its row terminated in the DB.
        try:
            await agents_forward._enqueue_lifecycle(  # pyright: ignore[reportUnknownMemberType]
                name,
                f"/api/agents/{agent_id}/terminate",
                {"force": True, "source": "machine-pause"},
            )
            if was_live:
                terminated += 1
        except Exception:
            await asyncio.to_thread(_force_mark_terminated_blocking, pool, agent_id)
            if was_live:
                force_marked += 1

    await asyncio.to_thread(_resolve_machine_alerts_blocking, pool, name)

    return MachinePauseResponse(
        name=name,
        paused=True,
        terminated_agents=terminated,
        force_marked_agents=force_marked,
        reassigned_tasks=reassigned,
        paused_at=paused_at,
        pause_reason=pause_reason if pause_reason is not None else req.reason,
    )


@router.post("/api/cluster/machines/{name}/resume", response_model=MachineResumeResponse)
def resume_cluster_machine(name: str, request: Request) -> MachineResumeResponse:
    """Restore a paused machine as a normal cluster member (`ava cluster resume`).

    Clears `paused_at`/`pause_reason`; the row's registration info (gateway_url
    / role / description) was never touched, so probing, the roster, rollout and
    spawn acceptance all resume immediately. The machine's own side needs no
    gateway action: when it is back online it re-runs `ava start`, whose
    `register_self()` refreshes its dial URL (its reachable address may have
    changed) and clears its `stopped_at` latch. If the machine's reachable
    address changed, the gateway's pg_hba must cover the new IP — see the ops checklist in the
    CLI output (`AVA_TRUSTED_CIDRS` + `ava cluster update --restart-only`).

    404 when the row does not exist; idempotent (resumed=False) when the
    machine was not paused.
    """
    pool = request.app.state.db_pool
    exists, _paused_at, _pause_reason = _read_machine_row(pool, name)
    if not exists:
        raise HTTPException(status_code=404, detail=f"no machine named {name!r}")
    resumed = machines.resume(name)
    return MachineResumeResponse(name=name, resumed=resumed)
