"""Read-only COS inventory for retention planning.

Mirrors ``GCSRetentionInventoryReader``: list every key under the selected
namespace (ListObjectsV2, paged), HEAD each object to resolve its identity
and metadata, and report anything without an adoptable MD5 identity as
unknown — the retention planner treats unknown names as blockers, never
as delete candidates. ``namespace="logical"`` selects the flat
``ava-logical/`` dump pool, classified by the shared name grammar; this
backend keeps no sidecars, so those objects are weak-evidence by
construction.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import MD5
from services.pitr.cos_client import CosClient, CosClientError, CosCredentials
from services.pitr.logical_dump_names import parse_dump_name, relative_name
from services.pitr.retention_inventory import (
    LOGICAL_NAMESPACE,
    PITR_NAMESPACE,
    InventorySnapshot,
    require_namespace,
)
from services.pitr.retention_manifest import RetentionObject


class CosRetentionInventoryReader:
    """Viewer-only inventory; this adapter deliberately has no write verb."""

    def __init__(
        self,
        *,
        credentials: CosCredentials,
        prefix: str,
        timeout_seconds: float = 300.0,
        namespace: str = PITR_NAMESPACE,
    ) -> None:
        require_namespace(namespace, prefix)
        self._client = CosClient(credentials, timeout_seconds=timeout_seconds)
        self._prefix = prefix.rstrip("/")
        self._namespace = namespace

    @classmethod
    def from_client(
        cls, client: CosClient, *, prefix: str, namespace: str = PITR_NAMESPACE
    ) -> CosRetentionInventoryReader:
        require_namespace(namespace, prefix)
        instance = cls.__new__(cls)
        instance._client = client
        instance._prefix = prefix.rstrip("/")
        instance._namespace = namespace
        return instance

    def snapshot(self) -> InventorySnapshot:
        if self._namespace == LOGICAL_NAMESPACE:
            return self._logical_snapshot()
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

    def _logical_snapshot(self) -> InventorySnapshot:
        """The flat logical namespace: strict naming plus the single-PUT stat."""
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for relative in self._keys():
            if relative.endswith(".ack.json"):
                # This backend keeps no sidecars; a foreign .ack.json is a
                # collision signal, not an inventory entry.
                unknown.append(relative)
                continue
            inner = relative_name(relative, root=self._prefix)
            if inner is None or parse_dump_name(inner) is None:
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
            try:
                objects.append(
                    RetentionObject(
                        relative,
                        row.etag,
                        row.size,
                        None,
                        "logical",
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
