"""Read-only exact-generation GCS inventory for retention planning."""

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
from services.pitr.retention_manifest import RetentionObject


@dataclass(frozen=True)
class InventorySnapshot:
    objects: tuple[RetentionObject, ...]
    unknown_names: tuple[str, ...]


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
    """Viewer-only inventory; this adapter deliberately has no write verb."""

    def __init__(self, *, project: str, bucket: str, prefix: str, credentials_file: Path) -> None:
        credentials = service_account.Credentials.from_service_account_file(str(credentials_file))
        self._bucket = cast(
            _Bucket, storage.Client(project=project, credentials=credentials).bucket(bucket)
        )
        self._prefix = prefix.rstrip("/")

    def snapshot(self) -> InventorySnapshot:
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
                        int(raw.generation),
                        int(raw.size),
                        archive_name,
                        kind,
                        str(raw.crc32c or ""),
                        tuple(sorted(metadata.items())),
                    )
                )
            except (TypeError, ValueError):
                unknown.append(raw.name)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))
