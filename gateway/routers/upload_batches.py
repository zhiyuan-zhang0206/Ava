"""Silent upload identities, receiving reservations and immutable file publication.

Receiving identities never expire: recovery supplies the original bytes. PostgreSQL
transaction locks serialize quota admission; create-only files also fence writers
that continue after their database connection has died.
"""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents import AgentNotFound
from base.agents.uploads import upload_url
from base.db import agent_exists
from base.db.transaction import write_transaction
from base.host.private_storage import create_private_bytes, ensure_private_dir
from gateway.schemas.uploads import UploadedBatch, UploadedFile

RESERVED_PREFIX = "ava-upload-"
type UploadItem = tuple[str, bytes, str]


def lock_agent(conn: Connection[Any], agent_id: int) -> None:
    """Use the same transaction-scoped quota gate for keyed and legacy writers."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"upload:{agent_id}",))


def check_quota(
    conn: Connection[Any],
    agent_id: int,
    directory: Path,
    incoming_bytes: int,
    incoming_files: int,
    max_bytes: int,
    max_files: int,
) -> None:
    """Count receiving objects once, including their not-yet-published bytes."""
    rows = conn.execute(
        "SELECT manifest FROM agent_upload_batches WHERE agent_id = %s AND receipt IS NULL",
        (agent_id,),
    ).fetchall()
    receiving = [item for row in rows for item in row[0]]
    owned = {item["stored_name"] for item in receiving}
    total_bytes = sum(item["size"] for item in receiving)
    total_files = len(receiving)
    for path in directory.iterdir():
        if path.name not in owned and path.is_file():
            total_bytes += path.stat().st_size
            total_files += 1
    if total_bytes + incoming_bytes > max_bytes:
        raise HTTPException(
            413, f"upload would exceed agent {agent_id}'s {max_bytes:,}-byte total quota"
        )
    if total_files + incoming_files > max_files:
        raise HTTPException(413, f"agent {agent_id} already holds {max_files} uploads")


def _fingerprint(batch: list[UploadItem], original_names: list[str], *, deliver: bool) -> str:
    manifest = [
        [name, len(contents), hashlib.sha256(contents).hexdigest(), content_type]
        for name, (_, contents, content_type) in zip(original_names, batch, strict=True)
    ]
    canonical = json.dumps([deliver, manifest], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _manifest(batch: list[UploadItem], original_names: list[str]) -> list[dict[str, Any]]:
    batch_id = uuid.uuid4().hex
    items: list[dict[str, Any]] = []
    for ordinal, (name, (_, contents, content_type)) in enumerate(
        zip(original_names, batch, strict=True)
    ):
        suffix = Path(name.replace("\\", "/")).suffix
        if (
            "\x00" in name
            or len(suffix.encode()) > 128
            or any(char in "?#%" or ord(char) < 32 or ord(char) == 127 for char in suffix)
        ):
            raise HTTPException(
                422, "upload suffix cannot be represented in an immutable object URL"
            )
        items.append(
            {
                "filename": name,
                "stored_name": f"{RESERVED_PREFIX}{batch_id}-{ordinal}{suffix}",
                "size": len(contents),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "content_type": content_type,
            }
        )
    return items


def _lookup(conn: Connection[Any], key: str, fingerprint: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        "SELECT request_fingerprint, manifest, receipt FROM agent_upload_batches WHERE operation_key = %s",
        (key,),
    ).fetchone()
    if row is not None and row[0] != fingerprint:
        raise HTTPException(409, "idempotency key was already used for a different upload manifest")
    return row


def _claim(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    directory: Path,
    batch: list[UploadItem],
    names: list[str],
    fingerprint: str,
    *,
    deliver: bool,
    max_bytes: int,
    max_files: int,
) -> tuple[Any, ...]:
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        row = _lookup(conn, key, fingerprint)
        if row is not None:
            return row
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
        if deliver:
            raise HTTPException(422, "keyed uploads currently require deliver=false")
        manifest = _manifest(batch, names)
        ensure_private_dir(directory)
        check_quota(
            conn,
            agent_id,
            directory,
            sum(len(item[1]) for item in batch),
            len(batch),
            max_bytes,
            max_files,
        )
        conn.execute(
            "INSERT INTO agent_upload_batches (operation_key, agent_id, request_fingerprint, manifest) "
            "VALUES (%s, %s, %s, %s)",
            (key, agent_id, fingerprint, Jsonb(manifest)),
        )
        return fingerprint, manifest, None


def _verify(path: Path, item: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise HTTPException(409, "immutable upload object is not a regular file")
    contents = path.read_bytes()
    if len(contents) != item["size"] or hashlib.sha256(contents).hexdigest() != item["sha256"]:
        raise HTTPException(409, "immutable upload object does not match its manifest")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_files(directory: Path, manifest: list[dict[str, Any]], batch: list[UploadItem]) -> None:
    """Publish without replacing or removing any final name, even on failure."""
    staging = ensure_private_dir(directory / ".batch-staging" / uuid.uuid4().hex)
    try:
        for ordinal, (item, (_, contents, _)) in enumerate(zip(manifest, batch, strict=True)):
            temporary = staging / str(ordinal)
            create_private_bytes(temporary, contents)
            final = directory / item["stored_name"]
            try:
                os.link(temporary, final)
            except FileExistsError:
                _verify(final, item)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
        _fsync_directory(directory.parent.parent)
    finally:
        # Never sweep another attempt: a writer can outlive its DB connection.
        shutil.rmtree(staging)


def save_keyed_batch(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    directory: Path,
    batch: list[UploadItem],
    names: list[str],
    *,
    deliver: bool,
    max_bytes: int,
    max_files: int,
) -> UploadedBatch:
    """Claim first, then publish and commit an immutable receipt in a second transaction."""
    fingerprint = _fingerprint(batch, names, deliver=deliver)
    claimed = _claim(
        pool,
        key,
        agent_id,
        directory,
        batch,
        names,
        fingerprint,
        deliver=deliver,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    if claimed[2] is not None:
        return UploadedBatch.model_validate(claimed[2])
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        row = _lookup(conn, key, fingerprint)
        assert row is not None  # noqa: S101 -- committed receiving identity is never removed
        if row[2] is not None:
            return UploadedBatch.model_validate(row[2])
        ensure_private_dir(directory)
        publish_files(directory, row[1], batch)
        receipt = UploadedBatch(
            files=[
                UploadedFile(
                    filename=item["filename"],
                    path=str(directory / item["stored_name"]),
                    url=upload_url(agent_id, item["stored_name"]),
                    size=item["size"],
                    content_type=item["content_type"],
                )
                for item in row[1]
            ]
        )
        conn.execute(
            "UPDATE agent_upload_batches SET receipt = %s, ready_at = now() WHERE operation_key = %s",
            (Jsonb(receipt.model_dump()), key),
        )
    return receipt
