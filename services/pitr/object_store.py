"""Owned immutable-object boundary for the PITR data plane.

The ACK is backend-agnostic: ``pin_token`` is the backend's own
pinned-read credential (GCS fills the object generation; Baidu Netdisk
fills ``fs_id`` + its encrypted server row digest — not the content
MD5, which is carried by ``checksum`` and verified from the downloaded
bytes), and ``checksum`` is an ``(algo, value)`` pair dispatched through
:mod:`services.pitr.checksums` (GCS verifies CRC32C, Baidu only exposes
MD5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from services.pitr.checksums import ObjectChecksum


@dataclass(frozen=True)
class RemoteObjectAck:
    """A store-verified identity for one immutable remote object."""

    object_name: str
    pin_token: str
    size: int
    checksum: ObjectChecksum
    metadata: Mapping[str, str]
    created: bool


class ObjectStore(Protocol):
    def stat(self, object_name: str) -> RemoteObjectAck | None:
        """Re-observe an object's current identity, or None when absent.

        The viewer-side activation proof uses this to confirm the durable
        ACK against the backend before cut-over. Not a publish verb.
        """
        ...

    def put_wal_ciphertext_if_absent(
        self,
        path: Path,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """Publish a bounded, seekable WAL ciphertext iff absent.

        ``path`` MUST be a real regular file: the SDK's resumable upload
        (> 8 MiB) seeks and re-reads its source, which only a seekable file
        supports (an in-memory encrypted stream raises UnsupportedOperation
        and every large WAL segment fails to upload — QA #920 block 1).
        Base backups must use the separate restartable-stream boundary added
        with base-chain support; they must never materialize through this API.
        """
        ...


class ObjectStoreError(RuntimeError):
    """A remote object operation failed."""


class TransientObjectStoreError(ObjectStoreError):
    """The operation may be retried by the outer uploader loop."""


class PermanentObjectStoreError(ObjectStoreError):
    """The operation cannot succeed without operator action."""
