"""Read-only COS inventory for retention planning.

Mirrors ``GCSRetentionInventoryReader``: list every key under the PITR
prefix (ListObjectsV2, paged), HEAD each object to resolve its identity
and metadata, and report anything without an adoptable MD5 identity as
unknown — the retention planner treats unknown names as blockers, never
as delete candidates.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import MD5
from services.pitr.cos_client import CosClient, CosClientError, CosCredentials
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import RetentionObject


class CosRetentionInventoryReader:
    """Viewer-only inventory; this adapter deliberately has no write verb."""

    def __init__(
        self, *, credentials: CosCredentials, prefix: str, timeout_seconds: float = 300.0
    ) -> None:
        self._client = CosClient(credentials, timeout_seconds=timeout_seconds)
        self._prefix = prefix.rstrip("/")

    @classmethod
    def from_client(cls, client: CosClient, *, prefix: str) -> CosRetentionInventoryReader:
        instance = cls.__new__(cls)
        instance._client = client
        instance._prefix = prefix.rstrip("/")
        return instance

    def snapshot(self) -> InventorySnapshot:
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for relative in self._keys():
            if relative.startswith(f"{self._prefix}/protected/"):
                continue
            if relative.endswith(".ack.json"):
                # This backend keeps no sidecars; a foreign .ack.json is a
                # collision signal, not an inventory entry.
                unknown.append(relative)
                continue
            try:
                row = self._client.head_object(relative)
            except CosClientError:
                unknown.append(relative)
                continue
            if row is None or "-" in row.etag:
                unknown.append(relative)
                continue
            metadata = row.metadata
            if relative.startswith(f"{self._prefix}/base/"):
                base_pattern = (
                    rf"{re.escape(self._prefix)}/base/"
                    r"[0-9]{8}T[0-9]{6}Z/[0-9a-f]{64}/base\.tar\.zst\.enc"
                )
                if re.fullmatch(base_pattern, relative) is None:
                    unknown.append(relative)
                    continue
                kind, archive_name = "base", None
            elif relative.startswith(f"{self._prefix}/wal/"):
                archive_name = metadata.get("ava-archive-name")
                if archive_name is None or not archive_name_is_valid(archive_name):
                    unknown.append(relative)
                    continue
                kind = "history" if archive_name.endswith(".history") else "wal"
                expected_name = f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"
                if relative != expected_name:
                    unknown.append(relative)
                    continue
            else:
                unknown.append(relative)
                continue
            try:
                objects.append(
                    RetentionObject(
                        relative,
                        row.etag,
                        row.size,
                        archive_name,
                        kind,
                        MD5,
                        row.etag,
                        tuple(sorted(metadata.items())),
                    )
                )
            except (TypeError, ValueError):
                unknown.append(relative)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))

    def _keys(self) -> Iterator[str]:
        return self._client.list_object_keys(f"{self._prefix}/")
