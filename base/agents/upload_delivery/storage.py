"""One quota owner for flat uploads and immutable delivered objects on native disk."""

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from psycopg import Connection

from base.agents.upload_delivery.models import (
    BATCH_ID_PATTERN,
    NAMESPACE,
    OBJECT_NAME_PATTERN,
    Manifest,
    Object,
    UploadDeliveryConflictError,
    UploadQuotaExceededError,
)
from base.host.private_storage import create_private_bytes, ensure_private_dir


def lock_agent(conn: Connection[Any], agent_id: int) -> None:
    # Conservatively serialize across machines; physical reservation accounting
    # still binds the actual storage directory, not the independent AVA_HOME.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"upload:{agent_id}",))


def check_quota(
    conn: Connection[Any],
    agent_id: int,
    directory: Path,
    machine: str,
    incoming_bytes: int,
    incoming_files: int,
    max_bytes: int,
    max_files: int,
    *,
    incoming_manifest: Manifest | None = None,
) -> None:
    owned: dict[str, int] = {}
    rows = conn.execute(
        "SELECT manifest FROM agent_upload_batches WHERE agent_id=%s AND receipt IS NULL",
        (agent_id,),
    ).fetchall()
    # Old receiving identities have no native root binding. Retain conservatively;
    # never erase or assign them to this unit by guessing an old writer is dead.
    for row in rows:
        for item in row[0]:
            owned[item["stored_name"]] = item["size"]
    rows = conn.execute(
        "SELECT manifest FROM upload_delivery_batches WHERE agent_id=%s AND storage_machine=%s "
        "AND storage_directory=%s AND ready_at IS NULL UNION ALL "
        "SELECT manifest FROM upload_delivery_copies WHERE agent_id=%s AND storage_machine=%s "
        "AND storage_directory=%s AND ready_at IS NULL",
        (agent_id, machine, str(directory.resolve()), agent_id, machine, str(directory.resolve())),
    ).fetchall()
    for row in rows:
        manifest = Manifest.model_validate(row[0])
        for item in manifest.objects:
            owned[f"{NAMESPACE}/{manifest.batch_id}/{item.name}"] = item.size
    total_bytes, total_files = _inventory(directory, owned)
    if incoming_manifest is not None:
        existing = _owned_incoming(directory, incoming_manifest, owned)
        incoming_bytes -= sum(existing)
        incoming_files -= len(existing)
    if total_bytes + incoming_bytes > max_bytes or total_files + incoming_files > max_files:
        raise UploadQuotaExceededError(f"upload exceeds agent {agent_id}'s native physical quota")


def _inventory(directory: Path, owned: dict[str, int]) -> tuple[int, int]:
    total_bytes, total_files = sum(owned.values()), len(owned)
    if directory.exists():
        paths = [path for path in directory.iterdir() if path.is_file()]
        hidden = directory / NAMESPACE
        if hidden.exists():
            paths.extend(path for path in hidden.glob("*/*") if _is_final_object(path))
        for path in paths:
            if path.relative_to(directory).as_posix() not in owned:
                total_bytes += path.stat().st_size
                total_files += 1
    return total_bytes, total_files


def _is_final_object(path: Path) -> bool:
    # create_private_bytes can leave .<name>.<uuid>.tmp on hard death. It is
    # staging, never a second final charged beside a receiving reservation.
    batch = path.parent.name
    return bool(
        re.fullmatch(BATCH_ID_PATTERN, batch)
        and re.fullmatch(OBJECT_NAME_PATTERN, path.name)
        and path.name.split(".", 1)[0].rsplit("-", 1)[0] == batch
        and path.is_file()
    )


def _owned_incoming(directory: Path, manifest: Manifest, owned: dict[str, int]) -> list[int]:
    existing: list[int] = []
    for item in manifest.objects:
        relative = f"{NAMESPACE}/{manifest.batch_id}/{item.name}"
        path = directory / relative
        if path.exists() or path.is_symlink():
            verify(path, item)
            existing.append(item.size)
        elif relative in owned:
            existing.append(item.size)
    return existing


def object_path(directory: Path, manifest: Manifest, item: Object) -> Path:
    path = directory / NAMESPACE / manifest.batch_id / item.name
    if path.resolve().parent != (directory.resolve() / NAMESPACE / manifest.batch_id):
        raise UploadDeliveryConflictError("immutable upload directory escapes its native root")
    return path


def verify(path: Path, item: Object) -> None:
    if path.is_symlink() or not path.is_file():
        raise UploadDeliveryConflictError("immutable upload object is not a regular file")
    contents = path.read_bytes()
    if len(contents) != item.size or hashlib.sha256(contents).hexdigest() != item.sha256:
        raise UploadDeliveryConflictError("immutable upload object differs from its fixed manifest")


def publish(directory: Path, manifest: Manifest, contents: list[bytes]) -> None:
    """Create only; each concurrent attempt cleans only its own primitive temp."""
    for item, data in zip(manifest.objects, contents, strict=True):
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise UploadDeliveryConflictError("received bytes differ from the fixed manifest")
        path = object_path(directory, manifest, item)
        ensure_private_dir(path.parent)
        try:
            create_private_bytes(path, data)
        except FileExistsError:
            verify(path, item)
    fsync_tree(directory, manifest)


def fsync_tree(directory: Path, manifest: Manifest) -> None:
    for path in (
        directory / NAMESPACE / manifest.batch_id,
        directory / NAMESPACE,
        directory,
        directory.parent,
        directory.parent.parent,
    ):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_all(directory: Path, manifest: Manifest) -> None:
    for item in manifest.objects:
        verify(object_path(directory, manifest, item), item)
    fsync_tree(directory, manifest)
