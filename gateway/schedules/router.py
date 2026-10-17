from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg import sql
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from base.cluster import session_name
from base.config import settings
from base.db.transaction import write_transaction
from base.paths import meiva_home
from gateway.agents.creation import create_and_launch_agent, guarded_draft_key
from gateway.http.model_catalog import require_model_catalog
from gateway.routers.receipts import creation as creation_receipts
from gateway.schedules import receipts, session_control
from ops.rpc_schemas import SpawnAgentRequest

router = APIRouter()


_SUMMARY_COLS = (
    "id, name, description, command, enabled, status, last_error, created_at, updated_at"
)
_FULL_COLS = _SUMMARY_COLS + ", script"


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


def _changed_fields(previous: tuple[Any, ...], fields: dict[str, Any]) -> dict[str, Any]:
    current = dict(
        zip(
            _UPDATABLE,
            (previous[1], previous[2], previous[9], previous[3], previous[4]),
            strict=True,
        )
    )
    return {col: fields[col] for col in _UPDATABLE if col in fields and fields[col] != current[col]}


def _edit_assignments(changed: dict[str, Any], *, code_changed: bool) -> list[sql.Composable]:
    assignments: list[sql.Composable] = [
        sql.SQL("{} = %s").format(sql.Identifier(col)) for col in changed
    ]
    assignments.append(sql.SQL("updated_at = now()"))
    if code_changed or "enabled" in changed:
        assignments.extend(
            [
                sql.SQL("desired_revision = desired_revision + 1"),
                sql.SQL("launch_count = 0"),
                sql.SQL("next_launch_at = NULL"),
            ]
        )
    return assignments


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def update_blocking(
    pool: ConnectionPool[Any], schedule_id: int, fields: dict[str, Any], key: str | None = None
) -> tuple[tuple[Any, ...], bool]:

    if "script" in fields:
        _validate_script(fields["script"])

    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "fields": fields})
            if replay is not None:
                return replay, False
            cur.execute(
                f"SELECT {_FULL_COLS} FROM schedules WHERE id = %s FOR UPDATE",  # noqa: S608
                (schedule_id,),
            )
            previous = cur.fetchone()
            if previous is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            changed = _changed_fields(previous, fields)
            if not changed:
                receipts.finish(conn, key, previous)
                return previous, False
            code_changed = "script" in changed or "command" in changed
            assignments = _edit_assignments(changed, code_changed=code_changed)
            cur.execute(
                sql.SQL("UPDATE schedules SET {} WHERE id = %s RETURNING {}").format(
                    sql.SQL(", ").join(assignments), sql.SQL(_FULL_COLS)
                ),
                (*changed.values(), schedule_id),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101
            if code_changed:
                cur.execute(
                    "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (schedule_id, row[9], row[3], "edit"),
                )
            needs_sync = (code_changed and row[4]) or "enabled" in changed
            if needs_sync:
                session_control.enqueue_in_transaction(conn, schedule_id)
            receipts.finish(conn, key, row)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {fields['name']!r} already exists"
        ) from exc
    return row, needs_sync


def _list_blocking(pool: ConnectionPool[Any]) -> list[ScheduleSummary]:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_SUMMARY_COLS} FROM schedules ORDER BY id DESC")  # noqa: S608
        return [_summary(r) for r in cur.fetchall()]


@router.get(
    "/api/schedules",
    description="List all schedules (enabled and disabled), newest first. Omits `script`.",
)
async def list_schedules(request: Request) -> list[ScheduleSummary]:

    return await asyncio.to_thread(_list_blocking, request.app.state.db_pool)


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def create_blocking(
    pool: ConnectionPool[Any], body: ScheduleCreate, key: str | None = None
) -> tuple[Any, ...]:

    _validate_script(body.script)
    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            created = creation_receipts.begin(
                conn, key, {"resource": "schedule", "body": body.model_dump()}
            )
            if created is not None:
                return (
                    created.resource_id,
                    body.name,
                    body.description,
                    body.command,
                    body.enabled,
                    "stopped",
                    None,
                    created.created_at,
                    created.updated_at,
                    body.script,
                )
            cur.execute(
                f"INSERT INTO schedules (name, description, script, command, enabled) "  # noqa: S608
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_FULL_COLS}",
                (body.name, body.description, body.script, body.command, body.enabled),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (row[0], body.script, body.command, "initial"),
            )
            creation_receipts.finish(conn, key, creation_receipts.Creation(row[0], row[7], row[8]))
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {body.name!r} already exists"
        ) from exc
    return row


@router.post(
    "/api/schedules",
    status_code=201,
    description="Create a schedule. 400 on a script syntax error, 409 on a name clash.",
)
async def create_schedule(request: Request, body: ScheduleCreate) -> ScheduleView:

    row = await asyncio.to_thread(
        create_blocking, request.app.state.db_pool, body, creation_receipts.operation_key(request)
    )

    return _view(row)


@router.get(
    "/api/schedules/{schedule_id}",
    description="Get one schedule (with its script). 404 if it does not exist.",
)
async def get_schedule(request: Request, schedule_id: int) -> ScheduleView:

    row = await asyncio.to_thread(fetch_full_blocking, request.app.state.db_pool, schedule_id)
    return _view(row)


@router.put(
    "/api/schedules/{schedule_id}",
    description="Partial-update a schedule — only the fields present in the body change. A\nscript/command change is snapshotted into schedule_versions and, if the\nschedule is enabled, applied immediately (the session is relaunched on the new\nscript). An enabled-flag value change also converges immediately, so disable\nreaps the session and enable creates it; a same-value update leaves the\nsession intact. 400 on a script syntax error, 404 if missing, 409 on a name\nclash.",
)
async def update_schedule(request: Request, schedule_id: int, body: ScheduleUpdate) -> ScheduleView:

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    row, needs_sync = await asyncio.to_thread(
        update_blocking,
        request.app.state.db_pool,
        schedule_id,
        fields,
        receipts.operation_key(request),
    )
    if needs_sync:
        await session_control.wait_consumed(request.app.state.db_pool, schedule_id)
    return _view(row)


def delete_blocking(pool: ConnectionPool[Any], schedule_id: int) -> None:

    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE id = %s RETURNING id", (schedule_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        session_control.enqueue_in_transaction(conn, schedule_id)


def _remove_schedule_dir(schedule_id: int) -> None:

    shutil.rmtree(meiva_home() / "schedules" / str(schedule_id), ignore_errors=True)


@router.delete(
    "/api/schedules/{schedule_id}",
    description="Delete a schedule. 404 if missing. The reconcile loop kills its (now\norphaned) session; its work dir is removed.",
)
async def delete_schedule(request: Request, schedule_id: int) -> dict[str, str]:

    pool = request.app.state.db_pool
    await asyncio.to_thread(delete_blocking, pool, schedule_id)
    await session_control.wait_consumed(pool, schedule_id)
    await asyncio.to_thread(_remove_schedule_dir, schedule_id)
    return {"status": "deleted"}


@router.post(
    "/api/schedules/{schedule_id}/start",
    description="Enable + launch the schedule now. 404 if missing.",
)
async def start_schedule(request: Request, schedule_id: int) -> ScheduleView:

    return await _control_and_sync(request, schedule_id, action="start")


@router.post(
    "/api/schedules/{schedule_id}/stop",
    description="Disable + kill the schedule's session now. 404 if missing.",
)
async def stop_schedule(request: Request, schedule_id: int) -> ScheduleView:

    return await _control_and_sync(request, schedule_id, action="stop")


@router.post(
    "/api/schedules/{schedule_id}/restart",
    description="Relaunch the schedule's session now (reloading its script), clearing crash\nbackoff. 404 if missing, 409 if the schedule is disabled.",
)
async def restart_schedule(request: Request, schedule_id: int) -> ScheduleView:

    return await _control_and_sync(request, schedule_id, action="restart")


def _strip_trailing_blank(lines_list: list[str]) -> list[str]:

    while lines_list and not lines_list[-1].strip():
        lines_list.pop()
    return lines_list


def _read_transcript_blocking(schedule_id: int, lines: int) -> list[str] | None:

    from base.sessions.backend import get_shell_backend

    log_path = get_shell_backend().session_log_path(session_name(f"schedule-{schedule_id}"))
    if log_path is None or not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.splitlines()[-lines:]


@router.get(
    "/api/schedules/{schedule_id}/logs",
    description="Recent output of the schedule. Live session -> its capture; a dead\nsession -> its PTY transcript file; otherwise the last crash traceback\n(`last_error`). 404 if the schedule is missing.",
)
async def get_schedule_logs(
    request: Request, schedule_id: int, lines: int = 200
) -> ScheduleLogsView:

    row = await asyncio.to_thread(fetch_full_blocking, request.app.state.db_pool, schedule_id)

    window = max(lines, 200)
    captured = await session_control.capture(schedule_id, window)
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


@router.get(
    "/api/schedules/{schedule_id}/runs",
    description="Recent run-history rows (newest first). Omit `limit` for the configured\ndefault window (``display.schedules_runs_default_limit`` - 50 out of the\nbox). 404 if the schedule is missing.",
)
async def get_schedule_runs(
    request: Request, schedule_id: int, limit: int | None = None
) -> list[ScheduleRunView]:

    if limit is None:
        limit = settings.display.schedules_runs_default_limit
    pool = request.app.state.db_pool
    await asyncio.to_thread(fetch_full_blocking, pool, schedule_id)
    return await asyncio.to_thread(_runs_blocking, pool, schedule_id, limit)


@router.post(
    "/api/keyed/v1/schedules/draft",
    description="Hand a natural-language request to an meiva-schedule-writer agent. Spawns the\nagent (which loads meiva-guide.schedules, clarifies, writes the script,\nand POSTs it back) and returns its id so the UI can open the conversation.\nReplays one raw draft intent and its original birth.",
)
async def guarded_draft_schedule(
    body: ScheduleDraftRequest, request: Request, key: str = Depends(guarded_draft_key)
) -> ScheduleDraftResponse:

    prompt = (
        "You are a schedule writer. Read and follow meiva.skills.meiva_guide.schedules to turn this "
        "request into a gateway-hosted schedule: clarify the trigger / skip / error-handling, "
        "write the script, and create it via POST /api/schedules. Request:\n\n" + body.nl
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="meiva-schedule-writer",
    )
    spawned = await create_and_launch_agent(
        body_obj,
        request.app.state.machine.name(),
        request.app.state.db_pool,
        request.app.state.db,
        request.app.state.bus,
        catalog=require_model_catalog(request),
        authority=request.app.state.config_authority,
        creation_key=key,
        creation_identity=body.model_dump(mode="json"),
    )
    return ScheduleDraftResponse(agent_id=spawned.id)


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def fetch_full_blocking(pool: ConnectionPool[Any], schedule_id: int) -> tuple[Any, ...]:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_FULL_COLS} FROM schedules WHERE id = %s",  # noqa: S608
            (schedule_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def control_blocking(
    pool: ConnectionPool[Any], schedule_id: int, action: str, key: str | None
) -> tuple[tuple[Any, ...], bool]:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "action": action})
        if replay is not None:
            return replay, False
        cur.execute(f"SELECT {_FULL_COLS} FROM schedules WHERE id = %s FOR UPDATE", (schedule_id,))  # noqa: S608
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        enabled = action != "stop"
        if action == "restart" and not row[4]:
            raise HTTPException(
                status_code=409, detail="schedule is disabled; start it instead of restarting"
            )
        needs_sync = (
            action == "restart"
            or row[4] != enabled
            or (enabled and row[5] in ("completed", "error"))
        )
        if needs_sync:
            cur.execute(
                f"UPDATE schedules SET enabled = %s, desired_revision = desired_revision + 1, "  # noqa: S608
                f"launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s RETURNING {_FULL_COLS}",
                (enabled, schedule_id),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101
            session_control.enqueue_in_transaction(conn, schedule_id)
        receipts.finish(conn, key, row)
    return row, needs_sync


async def _control_and_sync(request: Request, schedule_id: int, *, action: str) -> ScheduleView:
    pool = request.app.state.db_pool
    row, needs_sync = await asyncio.to_thread(
        control_blocking, pool, schedule_id, action, receipts.operation_key(request)
    )
    if needs_sync:
        await session_control.wait_consumed(pool, schedule_id)
    return _view(row)
