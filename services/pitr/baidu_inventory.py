"""Read-only Baidu Netdisk inventory for retention planning.

Mirrors ``GCSRetentionInventoryReader``: walk the app-root tree under
the PITR prefix (one recursive listing), resolve every object through
its sidecar, and report anything without a valid sidecar as unknown —
the retention planner treats unknown names as blockers, never as delete
candidates.
"""

from __future__ import annotations

import re
from typing import Any, cast

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.baidu_pcs import PcsError, RemoteFile
from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import RetentionObject
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
        objects: list[RetentionObject] = []
        unknown: list[str] = []
        for row in self._list_under_prefix():
            relative = _relative_name(self._store.app_root, row.path)
            if relative is None or row.isdir or relative.endswith(".ack.json"):
                continue
            if relative.startswith(f"{self._prefix}/protected/"):
                continue
            sidecar = self._read_sidecar_guarded(relative)
            if sidecar is None:
                unknown.append(relative)
                continue
            metadata = cast(dict[str, str], sidecar["metadata"])
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
                        str(sidecar["pin_token"]),
                        int(sidecar["size"]),
                        archive_name,
                        kind,
                        str(sidecar["checksum_algo"]),
                        str(sidecar["checksum_value"]),
                        tuple(sorted(metadata.items())),
                    )
                )
            except (KeyError, TypeError, ValueError):
                unknown.append(relative)
        return InventorySnapshot(tuple(sorted(objects)), tuple(sorted(unknown)))

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
                page = self._store._client().list_dir(directory, start=start, recursion=1)
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
