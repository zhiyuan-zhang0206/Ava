"""Read-only Aliyun OSS inventory for retention planning.

Mirrors the Baidu inventory: list the prefix, classify every object against
the exact base / WAL / protected layout, and report anything unresolvable as
unknown — the retention planner treats unknown names as blockers, never as
delete candidates.

OSS never exposes a whole-object digest for multipart uploads, so base
objects resolve their checksum and metadata through the ACK sidecar the
publisher wrote; WAL objects are single PUTs whose ETag is the content MD5.
Base objects additionally capture their sidecar's own delete identity, and
a sidecar whose host is already gone is surfaced as an orphan candidate
(design section 2.5): silently skipping sidecars would let orphans
accumulate forever.

``namespace="logical"`` selects the flat ``ava-logical/`` dump pool,
classified by the shared name grammar. Its objects publish through the
same multipart path, so the sidecar pair is the full-strength binding;
a single-PUT straggler without a sidecar resolves through its ETag MD5 and
is left for the policy to mark weak-evidence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import MD5
from services.pitr.logical_dump_names import parse_dump_name, relative_name
from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)
from services.pitr.oss_store import OSSObjectStore
from services.pitr.retention_inventory import (
    LOGICAL_NAMESPACE,
    PITR_NAMESPACE,
    InventorySnapshot,
    require_namespace,
)
from services.pitr.retention_manifest import (
    SIDECAR_SUFFIX,
    OrphanSidecar,
    RetentionObject,
    RetentionSidecar,
    SidecarPair,
)


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
        namespace: str = PITR_NAMESPACE,
    ) -> None:
        require_namespace(namespace, prefix)
        self._store = OSSObjectStore(
            endpoint=endpoint,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )
        self._prefix = prefix.rstrip("/")
        self._namespace = namespace

    @classmethod
    def from_store(
        cls, store: OSSObjectStore, *, prefix: str, namespace: str = PITR_NAMESPACE
    ) -> OSSRetentionInventoryReader:
        require_namespace(namespace, prefix)
        instance = cls.__new__(cls)
        instance._store = store
        instance._prefix = prefix.rstrip("/")
        instance._namespace = namespace
        return instance

    def snapshot(self) -> InventorySnapshot:
        names = [item.key for item in self._store.list_objects(f"{self._prefix}/")]
        present = set(names)
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        sidecar_pairs: list[SidecarPair] = []
        for name in names:
            if name.endswith(SIDECAR_SUFFIX):
                continue
            if name.startswith(f"{self._prefix}/protected/"):
                continue
            if self._namespace == LOGICAL_NAMESPACE:
                inner = relative_name(name, root=self._prefix)
                if inner is None or parse_dump_name(inner) is None:
                    unknown.append(name)
                    continue
                kind = "logical"
            elif re.fullmatch(_base_pattern(self._prefix), name) is not None:
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
            elif kind == "logical":
                row = self._retention_row_logical(name, object_row)
            else:
                row = self._retention_row_wal(name, object_row, metadata)
            if row is None:
                unknown.append(name)
                continue
            objects.append(row)
            if row.kind in {"base", "logical"} and f"{name}{SIDECAR_SUFFIX}" in present:
                pair = self._sidecar_pair(row)
                if pair is not None:
                    sidecar_pairs.append(pair)
        orphans = self._orphan_sidecars(present)
        return InventorySnapshot(
            tuple(sorted(objects)),
            tuple(sorted(unknown)),
            tuple(sorted(sidecar_pairs)),
            tuple(sorted(orphans)),
        )

    def _sidecar_pair(self, row: RetentionObject) -> SidecarPair | None:
        """Capture the base object's sidecar identity; any doubt yields no pair.

        The content is re-read for every base row (single-PUT rows never
        read it through ``stat``) and must bind to the host's live identity,
        so a foreign sidecar can never ride along with a deletable host.
        """
        try:
            content = self._store.read_sidecar(row.object_name)
            identity = self._store.sidecar_identity(row.object_name)
        except (TransientObjectStoreError, PermanentObjectStoreError):
            return None
        if content is None or identity is None:
            return None
        try:
            if str(content["pin_token"]) != row.pin_token or int(content["size"]) != row.size:
                return None
            sidecar = RetentionSidecar(
                f"{row.object_name}{SIDECAR_SUFFIX}", identity[0], identity[1]
            )
        except (KeyError, TypeError, ValueError):
            return None
        return SidecarPair(row.pin_token, sidecar)

    def _orphan_sidecars(self, present: set[str]) -> list[OrphanSidecar]:
        """Sidecars whose host is gone, within the base namespace only.

        OSS publishes sidecars exclusively for base-class objects, so a
        sidecar outside that namespace is foreign and stays untouched; so
        does any sidecar whose host listing survived (fail closed).
        """
        orphans: list[OrphanSidecar] = []
        for name in sorted(present):
            if not name.endswith(SIDECAR_SUFFIX):
                continue
            host = name[: -len(SIDECAR_SUFFIX)]
            if host.startswith(f"{self._prefix}/protected/"):
                continue
            if host in present:
                continue
            if self._namespace == LOGICAL_NAMESPACE:
                inner = relative_name(host, root=self._prefix)
                if inner is None or parse_dump_name(inner) is None:
                    continue
            elif re.fullmatch(_base_pattern(self._prefix), host) is None:
                continue
            observation = self._orphan_sidecar(host)
            if observation is not None:
                orphans.append(observation)
        return orphans

    def _orphan_sidecar(self, host: str) -> OrphanSidecar | None:
        """Reconstruct the gone host from the sidecar content, or None on doubt."""
        try:
            content = self._store.read_sidecar(host)
            identity = self._store.sidecar_identity(host)
        except (TransientObjectStoreError, PermanentObjectStoreError):
            return None
        if content is None or identity is None:
            return None
        try:
            metadata = tuple(sorted(cast("dict[str, str]", content["metadata"]).items()))
            host_row = RetentionObject(
                host,
                str(content["pin_token"]),
                int(content["size"]),
                None,
                "logical" if self._namespace == LOGICAL_NAMESPACE else "base",
                str(content["checksum_algo"]),
                str(content["checksum_value"]),
                metadata,
            )
            sidecar = RetentionSidecar(f"{host}{SIDECAR_SUFFIX}", identity[0], identity[1])
        except (KeyError, TypeError, ValueError):
            return None
        return OrphanSidecar(host_row, sidecar)

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

    def _retention_row_logical(
        self, name: str, object_row: RemoteObjectAck
    ) -> RetentionObject | None:
        """A logical dump resolves its checksum like a base object: single-PUT
        rows through the ETag MD5, multipart rows through the bound sidecar."""
        if object_row.checksum.algo != MD5 or not object_row.checksum.value:
            return None
        metadata = dict(object_row.metadata)
        return RetentionObject(
            name,
            object_row.pin_token,
            object_row.size,
            None,
            "logical",
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
