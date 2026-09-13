"""Read-only Baidu Netdisk inventory for retention planning.

Mirrors ``GCSRetentionInventoryReader``: walk the app-root tree under
the PITR prefix (one recursive listing), resolve every object through
its sidecar, and report anything without a valid sidecar as unknown —
the retention planner treats unknown names as blockers, never as delete
candidates.

Every live object also captures its sidecar's own delete identity (one
listing carries both rows), and a sidecar whose host is already gone is
surfaced as an orphan candidate for the policy's fail-closed range check
(design section 2.5).
"""

from __future__ import annotations

import re
from typing import Any, cast

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.baidu_pcs import PcsError, RemoteFile
from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import (
    SIDECAR_SUFFIX,
    OrphanSidecar,
    RetentionObject,
    RetentionSidecar,
    SidecarPair,
)
from services.pitr.token_manager import StoreTokenManager


class BaiduRetentionInventoryReader:
    """Viewer-only inventory; this adapter deliberately has no write verb."""

    def __init__(
        self,
        *,
        app_root: str,
        prefix: str,
        token_manager: StoreTokenManager,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = BaiduObjectStore(
            app_root=app_root, token_manager=token_manager, timeout_seconds=timeout_seconds
        )
        self._prefix = prefix.rstrip("/")

    def snapshot(self) -> InventorySnapshot:
        present: dict[str, RemoteFile] = {}
        for row in self._list_under_prefix():
            relative = _relative_name(self._store.app_root, row.path)
            if relative is None or row.isdir:
                continue
            existing = present.get(relative)
            if existing is None or row.fs_id > existing.fs_id:
                present[relative] = row
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        sidecar_pairs: list[SidecarPair] = []
        for relative in sorted(present):
            if relative.endswith(SIDECAR_SUFFIX):
                continue
            if relative.startswith(f"{self._prefix}/protected/"):
                continue
            sidecar = self._read_sidecar_guarded(relative)
            if sidecar is None:
                unknown.append(relative)
                continue
            metadata = cast(dict[str, str], sidecar["metadata"])
            classified = self._classify(relative, metadata)
            if classified is None:
                unknown.append(relative)
                continue
            kind, archive_name = classified
            try:
                row = RetentionObject(
                    relative,
                    str(sidecar["pin_token"]),
                    int(sidecar["size"]),
                    archive_name,
                    kind,
                    str(sidecar["checksum_algo"]),
                    str(sidecar["checksum_value"]),
                    tuple(sorted(metadata.items())),
                )
            except (KeyError, TypeError, ValueError):
                unknown.append(relative)
                continue
            objects.append(row)
            pair = self._sidecar_pair(row, present)
            if pair is not None:
                sidecar_pairs.append(pair)
        orphans = self._orphan_sidecars(present)
        return InventorySnapshot(
            tuple(sorted(objects)),
            tuple(sorted(unknown)),
            tuple(sorted(sidecar_pairs)),
            tuple(sorted(orphans)),
        )

    def _classify(self, relative: str, metadata: dict[str, str]) -> tuple[str, str | None] | None:
        """Map a managed object name to ``(kind, archive_name)``; None = unknown."""
        if relative.startswith(f"{self._prefix}/base/"):
            base_pattern = (
                rf"{re.escape(self._prefix)}/base/"
                r"[0-9]{8}T[0-9]{6}Z/[0-9a-f]{64}/base\.tar\.zst\.enc"
            )
            if re.fullmatch(base_pattern, relative) is None:
                return None
            return ("base", None)
        if relative.startswith(f"{self._prefix}/wal/"):
            archive_name = metadata.get("ava-archive-name")
            if archive_name is None or not archive_name_is_valid(archive_name):
                return None
            kind = "history" if archive_name.endswith(".history") else "wal"
            expected_name = f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"
            if relative != expected_name:
                return None
            return (kind, archive_name)
        return None

    def _sidecar_pair(
        self, row: RetentionObject, present: dict[str, RemoteFile]
    ) -> SidecarPair | None:
        sidecar_row = present.get(f"{row.object_name}{SIDECAR_SUFFIX}")
        if sidecar_row is None:
            return None
        try:
            sidecar = RetentionSidecar(
                f"{row.object_name}{SIDECAR_SUFFIX}",
                f"{sidecar_row.fs_id}:{sidecar_row.md5}",
                sidecar_row.size,
            )
        except ValueError:
            return None
        return SidecarPair(row.pin_token, sidecar)

    def _orphan_sidecars(self, present: dict[str, RemoteFile]) -> list[OrphanSidecar]:
        orphans: list[OrphanSidecar] = []
        for name in sorted(present):
            if not name.endswith(SIDECAR_SUFFIX):
                continue
            host = name[: -len(SIDECAR_SUFFIX)]
            if host.startswith(f"{self._prefix}/protected/"):
                continue
            if not host.startswith((f"{self._prefix}/base/", f"{self._prefix}/wal/")):
                continue
            if host in present:
                continue
            observation = self._orphan_sidecar(host, present[name])
            if observation is not None:
                orphans.append(observation)
        return orphans

    def _orphan_sidecar(self, host: str, sidecar_row: RemoteFile) -> OrphanSidecar | None:
        """Reconstruct the gone host from the sidecar content, or None on doubt."""
        content = self._read_sidecar_guarded(host)
        if content is None:
            return None
        metadata = cast(dict[str, str], content["metadata"])
        classified = self._classify(host, metadata)
        if classified is None:
            return None
        kind, archive_name = classified
        try:
            host_row = RetentionObject(
                host,
                str(content["pin_token"]),
                int(content["size"]),
                archive_name,
                kind,
                str(content["checksum_algo"]),
                str(content["checksum_value"]),
                tuple(sorted(metadata.items())),
            )
            sidecar = RetentionSidecar(
                f"{host}{SIDECAR_SUFFIX}",
                f"{sidecar_row.fs_id}:{sidecar_row.md5}",
                sidecar_row.size,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return OrphanSidecar(host_row, sidecar)

    def _read_sidecar_guarded(self, relative: str) -> dict[str, Any] | None:
        try:
            return self._store.read_sidecar(relative)
        except (PcsError, TransientObjectStoreError, PermanentObjectStoreError):
            return None

    def _list_under_prefix(self) -> list[RemoteFile]:
        rows: list[RemoteFile] = []
        start = 0
        directory = f"{self._store.app_root}/{self._prefix}"
        while True:
            try:
                page = self._store._client().list_all(directory, start=start)
            except PcsError:
                return rows
            rows.extend(page)
            if len(page) < 1000:
                return rows
            start += len(page)


def _relative_name(app_root: str, path: str) -> str | None:
    prefix = f"{app_root.rstrip('/')}/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :]
