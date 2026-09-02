"""Journal the existing rollout's verified candidate; never activate an image."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import cast

import psycopg

from shared.migrations import MIGRATIONS_DIR, _assert_migration_authority
from shared.platform import file_lock
from shared.private_storage import write_private_bytes
from shared.runtime_migration import ReleaseMigrationContext
from shared.runtime_release import ReleaseRejectedError, verify_release


def _receipt_path(context: ReleaseMigrationContext) -> Path:
    identity = f"{context.holder}\n{context.acquired_at.isoformat()}".encode()
    name = hashlib.sha256(identity).hexdigest()
    return context.home / "run" / f"release-candidate-{name}.json"


def record_candidate(
    conn: psycopg.Connection, context: ReleaseMigrationContext, *, schema_digest: str
) -> Path:
    """Validate before writing a secret-free, immutable per-operation receipt.

    Called from the verified candidate interpreter. The ordinary source checkout
    cannot claim another image's packaged migrations. This is journal evidence
    under the existing deployment lease, not another deployment authority.
    """
    context.assert_operation(conn)
    context.validate(MIGRATIONS_DIR)
    _assert_migration_authority(conn, context)
    release = context.release
    verify_release(
        release.root.parent,
        release.digest,
        manifest_digest=release.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    payload = {
        "version": 1,
        "home": str(context.home),
        "holder": context.holder,
        "acquired_at": context.acquired_at.isoformat(),
        "target_sha": context.target_sha,
        "artifact_digest": release.digest,
        "manifest_digest": release.manifest_digest,
        "platform": platform.platform(),
        "schema_digest": schema_digest,
    }
    path = _receipt_path(context)
    parent = path.parent
    if parent.resolve() != parent or parent.is_symlink():
        raise ReleaseRejectedError("candidate journal directory must be canonical")
    parent.mkdir(mode=0o700, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    lock = parent / "release-candidate.lock"
    if lock.is_symlink():
        raise ReleaseRejectedError("candidate journal lock must not be a symlink")
    with file_lock(lock, timeout_s=5):
        if path.is_symlink():
            raise ReleaseRejectedError("candidate receipt must not be a symlink")
        if path.exists():
            if path.read_bytes() != encoded:
                raise ReleaseRejectedError("operation already has a different candidate receipt")
            return path
        write_private_bytes(path, encoded)
    return path


def load_candidate(conn: psycopg.Connection, path: Path) -> ReleaseMigrationContext:
    """Revalidate persisted candidate evidence against the *current* operation."""
    if path.resolve(strict=True) != path or path.is_symlink():
        raise ReleaseRejectedError("candidate receipt must have a canonical path")
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    fields = {
        "version",
        "home",
        "holder",
        "acquired_at",
        "target_sha",
        "artifact_digest",
        "manifest_digest",
        "platform",
        "schema_digest",
    }
    if not isinstance(raw, dict):
        raise ReleaseRejectedError("unsupported candidate receipt")
    values = cast(dict[object, object], raw)
    if set(values) != fields or type(values["version"]) is not int or values["version"] != 1:
        raise ReleaseRejectedError("unsupported candidate receipt")
    payload: dict[str, str] = {}
    for key in fields - {"version"}:
        value = values[key]
        if not isinstance(value, str):
            raise ReleaseRejectedError("candidate receipt fields must be strings")
        payload[key] = value
    home = Path(payload["home"])
    release = verify_release(
        home / "releases",
        payload["artifact_digest"],
        manifest_digest=payload["manifest_digest"],
        platform_tag=platform.platform(),
        schema_digest=payload["schema_digest"],
    )
    context = ReleaseMigrationContext(
        release,
        home,
        payload["holder"],
        datetime.fromisoformat(payload["acquired_at"]),
        payload["target_sha"],
    )
    if path != _receipt_path(context) or payload["platform"] != platform.platform():
        raise ReleaseRejectedError("candidate receipt does not belong to this operation/platform")
    context.assert_operation(conn)
    context.validate(MIGRATIONS_DIR)
    _assert_migration_authority(conn, context)
    return context


def admit_start_candidate(path: Path) -> ReleaseMigrationContext:
    """Validate before start writes; a migration receipt is not a cutover permit."""
    from shared.db import connect

    with connect(direct=True) as connection:
        load_candidate(connection, path)
    raise ReleaseRejectedError(
        "release start requires prepared service closure, bootable LKG, and all-writer barrier"
    )
