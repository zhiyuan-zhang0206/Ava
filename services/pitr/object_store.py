"""Owned immutable-object boundary for the PITR data plane."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class RemoteObjectAck:
    object_name: str
    generation: int
    size: int
    crc32c: str
    metadata: Mapping[str, str]
    created: bool


class ObjectStore(Protocol):
    def put_stream_if_absent(
        self,
        open_source: Callable[[], BinaryIO],
        size: int,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck: ...

    def stat(self, object_name: str) -> RemoteObjectAck | None: ...


class ObjectStoreError(RuntimeError):
    """A remote object operation failed."""


class TransientObjectStoreError(ObjectStoreError):
    """The operation may be retried by the outer uploader loop."""


class PermanentObjectStoreError(ObjectStoreError):
    """The operation cannot succeed without operator action."""
