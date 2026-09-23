"""Per-agent shell monitor — GET /api/agents/{id}/shell/{sid}.

A tail of one persistent-shell session's terminal output, backing the
shell monitor page. The capture runs on the machine the agent runs on via
the `shell_capture` cluster op — the gateway never touches sessions itself, and
every machine (the gateway's own included) is dialed at its registered ops
URL, so a remote runner's shells are captured exactly like a local one's.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg_pool import ConnectionPool

from gateway.schemas import ShellCaptureResponse
from ops import cluster_rpc as _cluster_rpc
from shared.config import settings

router = APIRouter()

# Valid range for the ?lines= query parameter: protective constants (the
# default *window* is config - display.shell_capture_default_lines). Lower
# bound prevents pathological "only capture 1 line" which is never useful;
# upper bound guards against OOM from an accidental very large number. Both
# are evaluated at import for the Query annotation, so they cannot be
# dynamic (task #3696 exception inventory: KEEP).
_MIN_LINES = 50
_MAX_LINES = 2000

# Per-op deadline for the shell capture. The monitor page polls every 3s; a
# reachable runner answers in milliseconds, and an unreachable one fails the
# connect within this bound (same budget as the roster's status_probe). The
# page keeps showing its last-good capture while the poll errors ("Stale").
_CAPTURE_TIMEOUT_S = 3.0


@router.get("/api/agents/{agent_id}/shell/{session_id}")
async def get_agent_shell(
    agent_id: int,
    session_id: int,
    request: Request,
    lines: Annotated[int | None, Query(ge=_MIN_LINES, le=_MAX_LINES)] = None,
) -> ShellCaptureResponse:
    """Capture the most recent terminal output of one of an agent's persistent
    shells — the data the shell monitor page fetches on demand.

    Dispatches the `shell_capture` op to the machine the agent runs on
    (`agents_meta.machine`, resolved through the `machines` table to that
    host's ops server URL — one uniform path for every machine, the gateway's
    own box included). The runner resolves `session_id` against its live shell
    sessions for the agent, reconstructs the full session name (carrying
    the optional `-<name>` suffix), and captures the last
    `lines` lines. Omit `lines` for the configured default
    (``display.shell_capture_default_lines``, 200 out of the box).

    404 if the agent is unknown (no agents_meta row), if the agent has no live
    shell with that id on its machine, or if the capture fails (the session
    died between the probe and the capture). 503 if the agent's machine's ops
    server is unreachable — the shell may still exist, but this gateway cannot
    reach it right now.
    """
    if lines is None:
        lines = settings.display.shell_capture_default_lines
    machine = await asyncio.to_thread(_agent_machine_blocking, request.app.state.db_pool, agent_id)

    try:
        result = await _cluster_rpc.dispatch_to_machine(
            machine,
            "shell_capture",
            {"agent_id": agent_id, "session_id": session_id, "lines": lines},
            timeout_s=_CAPTURE_TIMEOUT_S,
        )
    except _cluster_rpc.ClusterOpUnreachable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {machine!r} ops server unreachable for shell_capture: {exc!s}",
        ) from exc
    except _cluster_rpc.ClusterOpFailed as exc:
        # The op ran on the agent's machine and reported failure — the usual
        # cause is "no live shell with that id" (capture_shell's
        # ShellNotFoundError) or the session dying mid-capture. Either way the
        # monitor page gets the same 404 it would on a local miss.
        raise HTTPException(
            status_code=404,
            detail=f"agent {agent_id} shell {session_id} capture failed: {exc.result!r}",
        ) from exc

    created_at = _parse_created_at(result.get("created_at"))
    expires_at, renewals, last_renewed_at = await asyncio.to_thread(
        _shell_ttl_row_blocking,
        request.app.state.db_pool,
        agent_id,
        session_id,
    )
    return ShellCaptureResponse(
        agent_id=agent_id,
        session_id=session_id,
        session_name=result["session_name"],
        lines=result["lines"],
        created_at=created_at,
        uptime_seconds=int(result.get("uptime_seconds") or 0),
        expires_at=expires_at,
        renewals=renewals,
        last_renewed_at=last_renewed_at,
    )


def _parse_created_at(value: object) -> datetime | None:
    """The capture op's launch epoch as a datetime; None when absent/unparsable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _shell_ttl_row_blocking(
    pool: ConnectionPool,
    agent_id: int,
    session_id: int,
) -> tuple[datetime | None, int, datetime | None]:
    """The session's TTL facts from `agent_shell_ttls`: (deadline, renewal
    count, last renewal).

    A session without a row has no shell TTL or renewals: (None, 0, None).
    Page and schedule sessions have their own lifecycle and no shell TTL row.
    The table lives in the gateway's own Postgres, so the merge happens here,
    mirroring the inspector's shell list enrichment."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at, renewals, last_renewed_at FROM agent_shell_ttls "
            "WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    if row is not None:
        return row[0], row[1], row[2]
    return None, 0, None


def _agent_machine_blocking(pool: ConnectionPool, agent_id: int) -> str:
    """Sync home-machine lookup — via to_thread (404 when unknown)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return row[0]
