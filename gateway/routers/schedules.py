"""Schedule CRUD + control — /api/schedules.

A schedule is a persistent, gateway-supervised session (a `script` + a `command`
to run it); the gateway's ScheduleManager keeps one session per enabled row
(gateway/schedule_manager.py). This router is the management surface: list /
create / get / update / delete, start / stop / restart, logs, run history, and a
`draft` endpoint that hands a natural-language request to an ava-schedule-writer agent.

The script is syntax-checked with `compile()` at the write boundary (400 on a
SyntaxError) so a broken script never reaches the runner; every script/command
change is snapshotted into `schedule_versions` for roll-back.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from gateway.routers.agents import create_and_launch_agent
from ops.rpc_schemas import SpawnAgentRequest
from shared.cluster import session_name
from shared.machine import machine_name
from shared.paths import ava_home

router = APIRouter()

# Summary (list) omits the potentially large `script`; the full view adds it.
_SUMMARY_COLS = (
    "id, name, description, command, enabled, status, last_error, created_at, updated_at"
)
_FULL_COLS = _SUMMARY_COLS + ", script"

# Fields a PUT may set, in the order they map to columns.
_UPDATABLE = ("name", "description", "script", "command", "enabled")

_DEFAULT_COMMAND = "python schedule.py"


class ScheduleSummary(BaseModel):
    id: int
    name: str
    description: str | None
    command: str
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ScheduleView(ScheduleSummary):
    script: str


class ScheduleCreate(BaseModel):
    name: str
    script: str
    command: str = _DEFAULT_COMMAND
    description: str | None = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    script: str | None = None
    command: str | None = None
    description: str | None = None
    enabled: bool | None = None


class ScheduleRunView(BaseModel):
    id: int
    ran_at: datetime
    ok: bool | None
    agent_id: int | None
    note: str | None


class ScheduleLogsView(BaseModel):
    # source: 'live' = live session capture; 'transcript' = the session's
    # PTY transcript file (a dead session's output survives there);
    # 'last_error' = the last crash traceback; 'none' = nothing to show yet.
    source: str
    lines: list[str]


class ScheduleDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language description of the scheduled task.")


class ScheduleDraftResponse(BaseModel):
    agent_id: int


def _summary(r: tuple[Any, ...]) -> ScheduleSummary:
    return ScheduleSummary(
        id=r[0],
        name=r[1],
        description=r[2],
        command=r[3],
        enabled=r[4],
        status=r[5],
        last_error=r[6],
        created_at=r[7],
        updated_at=r[8],
    )


def _view(r: tuple[Any, ...]) -> ScheduleView:
    return ScheduleView(**_summary(r).model_dump(), script=r[9])


def _validate_script(script: str) -> None:
    try:
        compile(script, "<schedule>", "exec")
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400, detail=f"script has a syntax error (line {exc.lineno}): {exc.msg}"
        ) from exc


def _update_blocking(
    pool: ConnectionPool[Any], schedule_id: int, fields: dict[str, Any]
) -> tuple[Any, ...]:
    """Sync partial-update (syntax check + UPDATE + version snapshot) — via to_thread."""
    if "script" in fields:
        _validate_script(fields["script"])

    set_parts = [f"{col} = %s" for col in _UPDATABLE if col in fields]
    values = [fields[col] for col in _UPDATABLE if col in fields]
    set_parts.append("updated_at = now()")

    try:
        with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"UPDATE schedules SET {', '.join(set_parts)} "  # noqa: S608 — set_parts from the _UPDATABLE whitelist
                f"WHERE id = %s RETURNING {_FULL_COLS}",
                (*values, schedule_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            code_changed = "script" in fields or "command" in fields
            if code_changed:
                cur.execute(
                    "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (schedule_id, row[9], row[3], "edit"),
                )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {fields['name']!r} already exists"
        ) from exc
    return row


def _list_blocking(pool: ConnectionPool[Any]) -> list[ScheduleSummary]:
    """Sync list query — runs via asyncio.to_thread (sync psycopg)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_SUMMARY_COLS} FROM schedules ORDER BY id DESC")  # noqa: S608
        return [_summary(r) for r in cur.fetchall()]


@router.get("/api/schedules")
async def list_schedules(request: Request) -> list[ScheduleSummary]:
    """List all schedules (enabled and disabled), newest first. Omits `script`."""
    return await asyncio.to_thread(_list_blocking, request.app.state.db_pool)


def _create_blocking(pool: ConnectionPool[Any], body: ScheduleCreate) -> tuple[Any, ...]:
    """Sync create (syntax check + INSERT + version snapshot) — via to_thread."""
    _validate_script(body.script)
    try:
        with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO schedules (name, description, script, command, enabled) "  # noqa: S608
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_FULL_COLS}",
                (body.name, body.description, body.script, body.command, body.enabled),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields a row
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (row[0], body.script, body.command, "initial"),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {body.name!r} already exists"
        ) from exc
    return row


@router.post("/api/schedules", status_code=201)
async def create_schedule(request: Request, body: ScheduleCreate) -> ScheduleView:
    """Create a schedule. 400 on a script syntax error, 409 on a name clash."""
    row = await asyncio.to_thread(_create_blocking, request.app.state.db_pool, body)
    # A newly-created enabled schedule is launched by the reconcile loop within a
    # poll interval; no explicit sync needed here.
    return _view(row)


@router.get("/api/schedules/{schedule_id}")
async def get_schedule(request: Request, schedule_id: int) -> ScheduleView:
    """Get one schedule (with its script). 404 if it does not exist."""
    row = await asyncio.to_thread(_fetch_full_blocking, request.app.state.db_pool, schedule_id)
    return _view(row)


@router.put("/api/schedules/{schedule_id}")
async def update_schedule(request: Request, schedule_id: int, body: ScheduleUpdate) -> ScheduleView:
    """Partial-update a schedule — only the fields present in the body change. A
    script/command change is snapshotted into schedule_versions and, if the
    schedule is enabled, applied immediately (the session is relaunched on the new
    script). 400 on a script syntax error, 404 if missing, 409 on a name clash."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    row = await asyncio.to_thread(_update_blocking, request.app.state.db_pool, schedule_id, fields)
    code_changed = "script" in fields or "command" in fields
    # row[4] = enabled. Reload the running session onto the new script now.
    if code_changed and row[4]:
        await request.app.state.schedule_manager.sync(schedule_id)
    return _view(row)


def _delete_blocking(pool: ConnectionPool[Any], schedule_id: int) -> None:
    """Sync delete (DELETE + 404 guard) — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE id = %s RETURNING id", (schedule_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")


def _remove_schedule_dir(schedule_id: int) -> None:
    """Sync work-dir removal — via to_thread (filesystem walk)."""
    shutil.rmtree(ava_home() / "schedules" / str(schedule_id), ignore_errors=True)


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: int) -> dict[str, str]:
    """Delete a schedule. 404 if missing. The reconcile loop kills its (now
    orphaned) session; its work dir is removed."""
    pool = request.app.state.db_pool
    await asyncio.to_thread(_delete_blocking, pool, schedule_id)
    await request.app.state.schedule_manager.sync(schedule_id)  # kill the orphan session now
    await asyncio.to_thread(_remove_schedule_dir, schedule_id)
    return {"status": "deleted"}


@router.post("/api/schedules/{schedule_id}/start")
async def start_schedule(request: Request, schedule_id: int) -> ScheduleView:
    """Enable + launch the schedule now. 404 if missing."""
    return await _set_enabled_and_sync(request, schedule_id, enabled=True)


@router.post("/api/schedules/{schedule_id}/stop")
async def stop_schedule(request: Request, schedule_id: int) -> ScheduleView:
    """Disable + kill the schedule's session now. 404 if missing."""
    return await _set_enabled_and_sync(request, schedule_id, enabled=False)


@router.post("/api/schedules/{schedule_id}/restart")
async def restart_schedule(request: Request, schedule_id: int) -> ScheduleView:
    """Relaunch the schedule's session now (reloading its script), clearing crash
    backoff. 404 if missing, 409 if the schedule is disabled."""
    pool = request.app.state.db_pool
    row = await asyncio.to_thread(_fetch_full_blocking, pool, schedule_id)
    if not row[4]:  # enabled
        raise HTTPException(
            status_code=409, detail="schedule is disabled; start it instead of restarting"
        )
    await request.app.state.schedule_manager.sync(schedule_id)
    return _view(await asyncio.to_thread(_fetch_full_blocking, pool, schedule_id))


def _strip_trailing_blank(lines_list: list[str]) -> list[str]:
    """Drop trailing blank rows from a captured/transcribed tail.

    The PTY screen model pads the display to the terminal height, so the last
    `lines` rows of a capture are blank once the runner's output has scrolled
    above them — `logs --lines 10` then shows nothing even though the session
    has output. The transcript file ends the same way (the login shell's final
    newline). The tail the user asked for is the last N *content* rows.
    """
    while lines_list and not lines_list[-1].strip():
        lines_list.pop()
    return lines_list


def _read_transcript_blocking(schedule_id: int, lines: int) -> list[str] | None:
    """Tail the schedule session's PTY transcript, or None when it has none.

    The session's pty host writes a byte transcript to
    `$AVA_HOME/logs/ava-schedule-<id>.out.log` that survives the session being
    reaped — a completed/crashed runner's output lives there after live
    capture has nothing left to show (the backend keeps no such file, so this is the
    PTY-era replacement for the lost scrollback).
    """
    from shared.session_backend import get_shell_backend

    log_path = get_shell_backend().session_log_path(session_name(f"schedule-{schedule_id}"))
    if log_path is None or not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.splitlines()[-lines:]


@router.get("/api/schedules/{schedule_id}/logs")
async def get_schedule_logs(
    request: Request, schedule_id: int, lines: int = 200
) -> ScheduleLogsView:
    """Recent output of the schedule. Live session -> its capture; a dead
    session -> its PTY transcript file; otherwise the last crash traceback
    (`last_error`). 404 if the schedule is missing."""
    row = await asyncio.to_thread(_fetch_full_blocking, request.app.state.db_pool, schedule_id)
    # Capture a window wide enough to cover the display padding: the PTY
    # screen model pads to the terminal height, so a narrow `lines` window
    # can be all blank rows even with the runner's output above it. Fetch a
    # wider tail, strip the blank padding, then trim to the requested count.
    window = max(lines, 200)
    captured = await request.app.state.schedule_manager.capture(schedule_id, window)
    if captured is not None:
        tail = _strip_trailing_blank(captured.splitlines())
        return ScheduleLogsView(source="live", lines=tail[-lines:] if lines else tail)
    transcript = await asyncio.to_thread(_read_transcript_blocking, schedule_id, window)
    if transcript:
        tail = _strip_trailing_blank(transcript)
        return ScheduleLogsView(source="transcript", lines=tail[-lines:] if lines else tail)
    last_error = row[6]
    if last_error:
        return ScheduleLogsView(source="last_error", lines=last_error.splitlines())
    return ScheduleLogsView(source="none", lines=[])


def _runs_blocking(
    pool: ConnectionPool[Any], schedule_id: int, limit: int
) -> list[ScheduleRunView]:
    """Sync run-history query — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, ran_at, ok, agent_id, note FROM schedule_runs "
            "WHERE schedule_id = %s ORDER BY ran_at DESC, id DESC LIMIT %s",
            (schedule_id, limit),
        )
        return [
            ScheduleRunView(id=r[0], ran_at=r[1], ok=r[2], agent_id=r[3], note=r[4])
            for r in cur.fetchall()
        ]


@router.get("/api/schedules/{schedule_id}/runs")
async def get_schedule_runs(
    request: Request, schedule_id: int, limit: int = 50
) -> list[ScheduleRunView]:
    """Recent run-history rows (newest first). 404 if the schedule is missing."""
    pool = request.app.state.db_pool
    await asyncio.to_thread(_fetch_full_blocking, pool, schedule_id)  # 404 guard
    return await asyncio.to_thread(_runs_blocking, pool, schedule_id, limit)


@router.post("/api/schedules/draft")
async def draft_schedule(body: ScheduleDraftRequest, request: Request) -> ScheduleDraftResponse:
    """Hand a natural-language request to an ava-schedule-writer agent. Spawns the
    agent (which loads the ava-schedule-writer skill, clarifies, writes the script,
    and POSTs it back) and returns its id so the UI can open the conversation."""
    prompt = (
        "You are a schedule writer. Read and follow ava.skills.ava_schedule_writer to turn this "
        "request into a gateway-hosted schedule: clarify the trigger / skip / error-handling, "
        "write the script, and create it via POST /api/schedules. Request:\n\n" + body.nl
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="ava-schedule-writer",
    )
    spawned = await create_and_launch_agent(body_obj, machine_name(), request.app.state.db_pool)
    return ScheduleDraftResponse(agent_id=spawned.id)


def _fetch_full_blocking(pool: ConnectionPool[Any], schedule_id: int) -> tuple[Any, ...]:
    """Sync full-row fetch + 404 guard — via to_thread (used by every read path)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_FULL_COLS} FROM schedules WHERE id = %s",  # noqa: S608 — _FULL_COLS is a fixed literal
            (schedule_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


def _set_enabled_blocking(pool: ConnectionPool[Any], schedule_id: int, *, enabled: bool) -> None:
    """Sync enabled-flag UPDATE + 404 guard — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET enabled = %s, updated_at = now() WHERE id = %s RETURNING id",
            (enabled, schedule_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")


async def _set_enabled_and_sync(
    request: Request, schedule_id: int, *, enabled: bool
) -> ScheduleView:
    pool = request.app.state.db_pool
    await asyncio.to_thread(_set_enabled_blocking, pool, schedule_id, enabled=enabled)
    await request.app.state.schedule_manager.sync(schedule_id)
    return _view(await asyncio.to_thread(_fetch_full_blocking, pool, schedule_id))
