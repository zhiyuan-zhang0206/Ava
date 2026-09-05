"""File upload endpoints — /api/agents/{agent_id}/uploads.

POST saves each uploaded file to ~/Downloads/AvaAgent-{agent_id}/. With the
default `deliver=true` it also notifies the agent with a single chat inbound
listing every saved path (the paperclip / drag-drop path for arbitrary files).
With `deliver=false` it saves silently and returns each file's reference url —
the native-image-attachment path, where the frontend then sends the url inside
the next multimodal message instead of as a standalone notification.

Cross-machine: the file physically lands on the gateway, but an agent that
runs on a remote runner cannot read the gateway's disk. When `deliver=true`
and the agent's machine is not this host, the gateway dispatches an
`upload_receive` op to that runner first, which pulls the file into the
runner's own ~/Downloads/AvaAgent-{agent_id}/; the notification then carries
the agent's local absolute path and machine name. Each failed pull retains
its own gateway path, explicitly labels the gateway machine, and includes
the authenticated gateway download route; partial failures never shift a
successful path onto another file.

GET serves a saved file back (image thumbnails in the timeline, and any client
that wants to fetch an upload by its reference url).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from psycopg_pool import ConnectionPool

from gateway.inbound_provenance import request_inbound_provenance
from gateway.routers._delivery import deliver_chat_inbound
from gateway.schemas import UploadedBatch, UploadedFile
from shared.agents import AgentNotFound
from shared.db import agent_exists
from shared.machine import machine_name
from shared.private_storage import ensure_private_dir, write_private_bytes
from shared.uploads import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    MAX_UPLOAD_BYTES,
    agent_upload_dir,
    render_safe_headers,
    resolve_upload_path,
    sanitize_upload_name,
    upload_quota_used,
    upload_url,
)

_log = logging.getLogger(__name__)

# Cache idle locks only. A held lock or one with queued waiters remains until a
# later lookup can evict it safely, so two uploads for one agent never split
# across mutexes.
_AGENT_LOCK_CACHE_MAX_ENTRIES = 4096
_agent_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
_locks_guard = asyncio.Lock()

router = APIRouter()

type _UploadBatchItem = tuple[str, bytes, str]


async def _agent_lock(agent_id: int) -> asyncio.Lock:
    """Return the stable in-process lock serializing one agent's uploads."""
    async with _locks_guard:
        lock = _agent_locks.get(agent_id)
        if lock is None:
            while len(_agent_locks) >= _AGENT_LOCK_CACHE_MAX_ENTRIES:
                for stale_agent_id, stale_lock in _agent_locks.items():
                    waiters = stale_lock._waiters
                    if stale_lock.locked() or waiters:
                        continue
                    del _agent_locks[stale_agent_id]
                    break
                else:
                    break
            lock = asyncio.Lock()
            _agent_locks[agent_id] = lock
        else:
            _agent_locks.move_to_end(agent_id)
        return lock


def _sweep_stale_upload_temps(temp_dir: Path) -> None:
    """Best-effort removal of crash leftovers directly inside an upload .tmp dir."""
    ensure_private_dir(temp_dir)
    for path in temp_dir.iterdir():
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                _log.warning("upload: could not remove stale temp %s", path, exc_info=True)


def _remove_upload_temps(paths: list[Path]) -> None:
    """Best-effort cleanup for this batch's temp and backup files."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _log.warning("upload: could not remove batch temp %s", path, exc_info=True)


def _rollback_upload_batch(
    temp_paths: list[Path], backups: dict[Path, Path], created: set[Path], promoted: set[Path]
) -> None:
    """Restore overwritten files and remove final names created by a failed batch."""
    for dest in promoted:
        backup = backups.get(dest)
        try:
            if backup is not None and backup.is_file():
                os.replace(backup, dest)  # noqa: PTH105 -- required atomic restore
            elif dest in created:
                dest.unlink(missing_ok=True)
        except OSError:
            _log.warning("upload: could not roll back %s", dest, exc_info=True)
    _remove_upload_temps(temp_paths)


def _per_file_quota_error(safe_name: str) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"upload {safe_name!r} exceeds the {MAX_UPLOAD_BYTES:,}-byte per-file limit",
    )


def _agent_bytes_quota_error(agent_id: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=(
            f"upload would exceed agent {agent_id}'s {MAX_AGENT_UPLOAD_BYTES:,}-byte total quota"
        ),
    )


def _agent_files_quota_error(agent_id: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"agent {agent_id} already holds {MAX_AGENT_UPLOAD_FILES} uploads",
    )


async def _read_upload_batch(files: list[UploadFile]) -> tuple[list[_UploadBatchItem], int]:
    """Read and size-check every file before the batch can write a final name."""
    batch_bytes = 0
    batch: list[_UploadBatchItem] = []
    for file in files:
        safe_name = sanitize_upload_name(file.filename or "untitled")
        contents = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(contents) > MAX_UPLOAD_BYTES:
            raise _per_file_quota_error(safe_name)
        batch_bytes += len(contents)
        batch.append((safe_name, contents, file.content_type or "application/octet-stream"))
    return batch, batch_bytes


async def _commit_upload_batch(
    dest_dir: Path, temp_dir: Path, batch: list[_UploadBatchItem]
) -> None:
    """Stage a fully validated batch, then atomically promote its final names."""
    temp_paths: list[Path] = []
    backups: dict[Path, Path] = {}
    created: set[Path] = set()
    promoted: set[Path] = set()
    try:
        for _safe_name, contents, _content_type in batch:
            temp_path = temp_dir / uuid.uuid4().hex
            await asyncio.to_thread(write_private_bytes, temp_path, contents)
            temp_paths.append(temp_path)

        destinations = [
            (temp_path, dest_dir / safe_name)
            for temp_path, (safe_name, _, _) in zip(temp_paths, batch, strict=True)
        ]
        for dest in {dest for _, dest in destinations}:
            if await asyncio.to_thread(dest.is_file):
                backup = temp_dir / uuid.uuid4().hex
                backups[dest] = backup
                await asyncio.to_thread(os.link, dest, backup)
            elif not await asyncio.to_thread(dest.exists):
                created.add(dest)

        for temp_path, dest in destinations:
            await asyncio.to_thread(os.replace, temp_path, dest)
            promoted.add(dest)
    except BaseException:
        await asyncio.to_thread(_rollback_upload_batch, temp_paths, backups, created, promoted)
        raise
    finally:
        await asyncio.to_thread(_remove_upload_temps, [*temp_paths, *backups.values()])


@router.post("/api/agents/{agent_id}/uploads", response_model=UploadedBatch)
async def upload_files(
    agent_id: int,
    request: Request,
    files: list[UploadFile] = File(...),  # noqa: B008
    deliver: bool = Query(default=True),  # noqa: FBT001 — FastAPI query param, not a flag arg
) -> UploadedBatch:
    """Upload one or more files in a single batch. Each file is saved to
    ~/Downloads/AvaAgent-{agent_id}/ and returned with its reference url.

    `deliver=true` (default) inserts a single inbound listing every saved path,
    so the agent learns about the whole batch at once. `deliver=false` saves
    silently (no inbound) — the caller carries the returned urls into a later
    multimodal message.

    response_model makes FastAPI validate the return + emit the schema into
    OpenAPI components; the frontend codegen auto-syncs to types-generated.ts."""

    # Check agent exists first (fail fast before writing any file)
    await asyncio.to_thread(_agent_exists_blocking, request.app.state.db_pool, agent_id)

    dest_dir = ensure_private_dir(agent_upload_dir(agent_id))

    lock = await _agent_lock(agent_id)
    async with lock:
        temp_dir = dest_dir / ".tmp"
        await asyncio.to_thread(_sweep_stale_upload_temps, temp_dir)

        # The endpoint is the gateway's one authenticated user->disk write
        # surface, so the per-agent lock makes this baseline and batch commit
        # atomic relative to every other upload for this agent.
        quota_bytes, quota_files = await asyncio.to_thread(upload_quota_used, dest_dir)
        batch, batch_bytes = await _read_upload_batch(files)
        if quota_bytes + batch_bytes > MAX_AGENT_UPLOAD_BYTES:
            raise _agent_bytes_quota_error(agent_id)
        if quota_files + len(batch) > MAX_AGENT_UPLOAD_FILES:
            raise _agent_files_quota_error(agent_id)
        await _commit_upload_batch(dest_dir, temp_dir, batch)

        saved = [
            UploadedFile(
                filename=safe_name,
                path=str(dest_dir / safe_name),
                url=upload_url(agent_id, safe_name),
                size=len(contents),
                content_type=content_type,
            )
            for safe_name, contents, content_type in batch
        ]

    if deliver:
        # One inbound for the whole batch. When the agent runs on a remote
        # runner, first pull the files onto that host so the message can carry
        # the agent's LOCAL absolute path (the address it can act on) — the
        # gateway's own path is meaningless on another machine. A runner that
        # is unreachable does NOT fail the upload: identify the gateway copy
        # and its download route without pretending that path is runner-local.
        target_machine, local_paths = await _pull_uploads_to_agent_machine(
            request, agent_id, [f.filename for f in saved]
        )
        locations = [
            _upload_location(f, target_machine, local_paths.get(f.filename)) for f in saved
        ]
        if len(saved) == 1:
            message = f"File uploaded: {locations[0]}"
        else:
            message = f"{len(saved)} files uploaded:\n" + "\n".join(
                f"- {location}" for location in locations
            )
        await deliver_chat_inbound(
            request.app.state.db_pool,
            agent_id,
            prepare=lambda _conn: message,
            provenance=request_inbound_provenance(request),
        )

    return UploadedBatch(files=saved)


def _upload_location(file: UploadedFile, target_machine: str, local_path: str | None) -> str:
    """Describe one file's actual location and the gateway route without a confirmed local copy."""
    if local_path is not None:
        return f"{local_path} (machine: {target_machine}, {file.size} bytes)"
    return (
        f"{file.path} (machine: {machine_name()}, {file.size} bytes; "
        f"local copy unconfirmed on {target_machine or 'unknown machine'}; gateway download: {file.url})"
    )


@router.get("/api/agents/{agent_id}/uploads/{filename}")
def serve_upload(agent_id: int, filename: str, request: Request) -> FileResponse:
    """Serve one saved upload back (image thumbnails in the timeline).

    Traversal-safe: `resolve_upload_path` refuses any name escaping the agent's
    upload dir. 404 when the agent or the file does not exist.
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
    try:
        path = resolve_upload_path(agent_id, filename)
    except ValueError as e:
        # A traversal attempt is a bad request, not a server error.
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.is_file():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"upload {filename!r} not found")
    return FileResponse(path, headers=render_safe_headers(filename))


def _agent_machine_blocking(pool: ConnectionPool, agent_id: int) -> str:
    """Sync home-machine lookup for uploads — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]


async def _pull_uploads_to_agent_machine(
    request: Request, agent_id: int, names: list[str]
) -> tuple[str, dict[str, str]]:
    """Pull the just-saved uploads onto the agent's own machine, if remote.

    Returns the target machine and the local paths keyed by filename. A missing
    key means that file has no confirmed local copy; subsequent successful
    pulls retain their own keys. Same-machine uploads already have local copies.

    Best-effort by design: a failed pull (runner offline / mid-rollout /
    unregistered) must not fail the upload itself. The file is already safe
    on the gateway, and the URL in the message remains the uniform address.
    """
    machine = await asyncio.to_thread(_agent_machine_blocking, request.app.state.db_pool, agent_id)
    if not machine or machine == "unknown":
        return machine, {}

    if machine == machine_name():
        # Same host — the gateway path IS the agent's local path.
        return machine, {name: str(agent_upload_dir(agent_id) / name) for name in names}

    from ops import cluster_rpc

    pulled: dict[str, str] = {}
    for name in names:
        try:
            result = await cluster_rpc.dispatch_to_machine(
                target_machine=machine,
                kind="upload_receive",
                payload={"agent_id": agent_id, "name": name},
                # A large file pull (video / archive) over the private network
                # can exceed the default 30s op deadline; give it headroom.
                timeout_s=120.0,
            )
            pulled[name] = result["path"]
        except Exception as exc:  # ClusterOpUnreachable / ClusterOpFailed / wire errors
            _log.warning(
                "upload: pull %s to machine=%r failed (%r) — file stays on the "
                "gateway, message identifies its location and download route",
                name,
                machine,
                exc,
            )
    return machine, pulled


def _agent_exists_blocking(pool: ConnectionPool, agent_id: int) -> None:
    """Sync agent-exists guard for uploads — via to_thread."""
    with pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
