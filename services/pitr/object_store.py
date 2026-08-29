"""Owned immutable-object boundary for the PITR data plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RemoteObjectAck:
    object_name: str
    generation: int
    size: int
    crc32c: str
    metadata: Mapping[str, str]
    created: bool


class ObjectStore(Protocol):
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
