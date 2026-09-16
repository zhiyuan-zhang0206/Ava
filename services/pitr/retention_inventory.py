"""Read-only exact-generation GCS inventory for retention planning.

One class serves both deletion surfaces, selected by ``namespace``: the
PITR prefix (base / WAL layout, the default) and the flat logical dump
namespace (``ava-logical/``, classified by the shared name grammar). The
logical namespace keeps no sidecars on this backend, so its objects are
weak-evidence by construction -- strict naming plus the live stat.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from google.cloud import storage
from google.oauth2 import service_account

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import CRC32C
from services.pitr.logical_dump_names import parse_dump_name, relative_name
from services.pitr.retention_manifest import OrphanSidecar, RetentionObject, SidecarPair

PITR_NAMESPACE = "pitr"
LOGICAL_NAMESPACE = "logical"
NAMESPACES = (PITR_NAMESPACE, LOGICAL_NAMESPACE)


def require_namespace(namespace: str, prefix: str) -> None:
    """Validate one reader's namespace selection and namespace root."""
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown retention inventory namespace {namespace!r}")
    if not prefix:
        raise RuntimeError("retention inventory prefix is missing")


@dataclass(frozen=True)
class InventorySnapshot:
    objects: tuple[RetentionObject, ...]
    unknown_names: tuple[str, ...]
    sidecar_pairs: tuple[SidecarPair, ...] = ()
    """Bound sidecars observed beside a live host object (canonical order)."""

    orphan_sidecars: tuple[OrphanSidecar, ...] = ()
    """Sidecars whose host is already gone (canonical order)."""


class RetentionInventoryReader(Protocol):
    def snapshot(self) -> InventorySnapshot: ...


class _Blob(Protocol):
    name: str
    generation: int | str | None
    size: int | str | None
    crc32c: str | None
    metadata: Mapping[str, str] | None


class _Bucket(Protocol):
    def list_blobs(self, *, prefix: str) -> object: ...


class GCSRetentionInventoryReader:
    """Viewer-only inventory; this adapter deliberately has no write verb (deletion lives in the separate retention-delete role, off by default)."""

    def __init__(
        self,
        *,
        project: str,
        bucket: str,
        prefix: str,
        credentials_file: Path,
        namespace: str = PITR_NAMESPACE,
    ) -> None:
        require_namespace(namespace, prefix)
        credentials = service_account.Credentials.from_service_account_file(str(credentials_file))
        self._bucket = cast(
            _Bucket, storage.Client(project=project, credentials=credentials).bucket(bucket)
        )
        self._prefix = prefix.rstrip("/")
        self._namespace = namespace

    @classmethod
    def from_bucket(
        cls, bucket: _Bucket, *, prefix: str, namespace: str = PITR_NAMESPACE
    ) -> GCSRetentionInventoryReader:
        """Construct around an injected transport for contract tests."""
        require_namespace(namespace, prefix)
        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._prefix = prefix.rstrip("/")
        instance._namespace = namespace
        return instance

    def snapshot(self) -> InventorySnapshot:
        if self._namespace == LOGICAL_NAMESPACE:
            return self._logical_snapshot()
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for raw in cast(list[_Blob], self._bucket.list_blobs(prefix=f"{self._prefix}/")):
            metadata = dict(raw.metadata or {})
            if raw.generation is None or raw.size is None:
                unknown.append(raw.name)
                continue
            if raw.name.startswith(f"{self._prefix}/base/"):
                base_pattern = (
                    rf"{re.escape(self._prefix)}/base/"
                    r"[0-9]{8}T[0-9]{6}Z/[0-9a-f]{64}/base\.tar\.zst\.enc"
                )
                if re.fullmatch(base_pattern, raw.name) is None:
                    unknown.append(raw.name)
                    continue
                kind, archive_name = "base", None
            elif raw.name.startswith(f"{self._prefix}/wal/"):
                archive_name = metadata.get("ava-archive-name")
                if archive_name is None or not archive_name_is_valid(archive_name):
                    unknown.append(raw.name)
                    continue
                kind = "history" if archive_name.endswith(".history") else "wal"
                expected_name = f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"
                if raw.name != expected_name:
                    unknown.append(raw.name)
                    continue
            elif raw.name.startswith(f"{self._prefix}/protected/"):
                continue
            else:
                unknown.append(raw.name)
                continue
            try:
                objects.append(
                    RetentionObject(
                        raw.name,
                        str(raw.generation),
                        int(raw.size),
                        archive_name,
                        kind,
                        CRC32C,
                        str(raw.crc32c or ""),
                        tuple(sorted(metadata.items())),
                    )
                )
            except (TypeError, ValueError):
                unknown.append(raw.name)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))

    def _logical_snapshot(self) -> InventorySnapshot:
        """The flat logical namespace: strict naming plus the live stat."""
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for raw in cast(list[_Blob], self._bucket.list_blobs(prefix=f"{self._prefix}/")):
            metadata = dict(raw.metadata or {})
            if raw.generation is None or raw.size is None:
                unknown.append(raw.name)
                continue
            relative = relative_name(raw.name, root=self._prefix)
            if relative is None or parse_dump_name(relative) is None:
                unknown.append(raw.name)
                continue
            try:
                objects.append(
                    RetentionObject(
                        raw.name,
                        str(raw.generation),
                        int(raw.size),
                        None,
                        "logical",
                        CRC32C,
                        str(raw.crc32c or ""),
                        tuple(sorted(metadata.items())),
                    )
                )
            except (TypeError, ValueError):
                unknown.append(raw.name)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))
