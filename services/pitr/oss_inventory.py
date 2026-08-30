"""Read-only Aliyun OSS inventory for retention planning.

Mirrors the Baidu inventory: list the prefix, classify every object against
the exact base / WAL / protected layout, and report anything unresolvable as
unknown — the retention planner treats unknown names as blockers, never as
delete candidates.

OSS never exposes a whole-object digest for multipart uploads, so base
objects resolve their checksum and metadata through the ACK sidecar the
publisher wrote; WAL objects are single PUTs whose ETag is the content MD5.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import MD5
from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)
from services.pitr.oss_store import OSSObjectStore
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import RetentionObject


class OSSRetentionInventoryReader:
    """Viewer-only inventory; this adapter deliberately has no write verb."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        prefix: str,
        credentials_file: str | Path,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = OSSObjectStore(
            endpoint=endpoint,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )
        self._prefix = prefix.rstrip("/")

    @classmethod
    def from_store(cls, store: OSSObjectStore, *, prefix: str) -> OSSRetentionInventoryReader:
        instance = cls.__new__(cls)
        instance._store = store
        instance._prefix = prefix.rstrip("/")
        return instance

    def snapshot(self) -> InventorySnapshot:
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for item in self._store.list_objects(f"{self._prefix}/"):
            name = item.key
            if name.endswith(".ack.json"):
                continue
            if name.startswith(f"{self._prefix}/protected/"):
                continue
            if re.fullmatch(_base_pattern(self._prefix), name) is not None:
                kind = "base"
            elif name.startswith(f"{self._prefix}/wal/"):
                kind = None  # validated after metadata read
            else:
                unknown.append(name)
                continue
            try:
                object_row = self._store.stat(name)
            except (TransientObjectStoreError, PermanentObjectStoreError):
                unknown.append(name)
                continue
            if object_row is None:
                unknown.append(name)
                continue
            metadata = dict(object_row.metadata)
            if kind == "base":
                row = self._retention_row_base(name, object_row, metadata)
            else:
                row = self._retention_row_wal(name, object_row, metadata)
            if row is None:
                unknown.append(name)
            else:
                objects.append(row)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))

    def _retention_row_base(
        self, name: str, object_row: RemoteObjectAck, metadata: dict[str, str]
    ) -> RetentionObject | None:
        if object_row.checksum.algo != MD5 or not object_row.checksum.value:
            return None
        return RetentionObject(
            name,
            object_row.pin_token,
            object_row.size,
            None,
            "base",
            object_row.checksum.algo,
            object_row.checksum.value,
            tuple(sorted(metadata.items())),
        )

    def _retention_row_wal(
        self, name: str, object_row: RemoteObjectAck, metadata: dict[str, str]
    ) -> RetentionObject | None:
        archive_name = metadata.get("ava-archive-name")
        if archive_name is None or not archive_name_is_valid(archive_name):
            return None
        kind = "history" if archive_name.endswith(".history") else "wal"
        expected_name = f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"
        if name != expected_name:
            return None
        if object_row.checksum.algo != MD5 or not object_row.checksum.value:
            return None
        return RetentionObject(
            name,
            object_row.pin_token,
            object_row.size,
            archive_name,
            kind,
            object_row.checksum.algo,
            object_row.checksum.value,
            tuple(sorted(metadata.items())),
        )


def _base_pattern(prefix: str) -> str:
    return (
        rf"{re.escape(prefix)}/base/"
        r"[0-9]{8}T[0-9]{6}Z/[0-9a-f]{64}/base\.tar\.zst\.enc"
    )
