from __future__ import annotations

import base64
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

import google_crc32c
import pytest
from google.api_core.exceptions import PreconditionFailed

from services.pitr.base_object_store import GCSRestartableStreamingObjectStore
from services.pitr.object_store import PermanentObjectStoreError


@dataclass
class Source:
    value: bytes
    opens: int = 0

    @property
    def ciphertext_size(self) -> int:
        return len(self.value)

    @property
    def ciphertext_crc32c(self) -> str:
        checksum = google_crc32c.Checksum()
        checksum.update(self.value)  # pyright: ignore[reportUnknownMemberType]
        return base64.b64encode(checksum.digest()).decode("ascii")

    def iter_chunks(self) -> Iterable[bytes]:
        self.opens += 1
        yield self.value[:2]
        yield self.value[2:]


class Writer(AbstractContextManager[BytesIO]):
    def __init__(self, blob: Blob) -> None:
        self._blob = blob
        self._stream = BytesIO()

    def __enter__(self) -> BytesIO:
        return self._stream

    def __exit__(self, *_args: object) -> None:
        self._blob.value = self._stream.getvalue()


class Blob:
    def __init__(self, name: str, *, fail_precondition: bool = False) -> None:
        self.name = name
        self.generation = 1
        self.size: int | None = None
        self.crc32c: str | None = None
        self.metadata: dict[str, str] = {}
        self.value = b""
        self.fail_precondition = fail_precondition

    def open(self, _mode: str, **_kwargs: object):
        if self.fail_precondition:
            raise PreconditionFailed("exists")
        return Writer(self)

    def reload(self, **_kwargs: object) -> None:
        self.size = len(self.value)
        checksum = google_crc32c.Checksum()
        checksum.update(self.value)  # pyright: ignore[reportUnknownMemberType]
        self.crc32c = base64.b64encode(checksum.digest()).decode("ascii")


class Bucket:
    def __init__(self, blob: Blob, existing: Blob | None = None) -> None:
        self._blob = blob
        self._existing = existing

    def blob(self, _name: str) -> Blob:
        return self._blob

    def get_blob(self, _name: str, **_kwargs: object) -> Blob | None:
        return self._existing


def test_stream_upload_verifies_exact_remote_identity() -> None:
    source = Source(b"ciphertext")
    blob = Blob("base/object")
    store = GCSRestartableStreamingObjectStore.from_bucket_client(cast(Any, Bucket(blob)))
    metadata = {"candidate": "sha"}

    ack = store.put_base_if_absent(source=source, object_name="base/object", metadata=metadata)

    assert ack.size == len(source.value)
    assert ack.crc32c == source.ciphertext_crc32c
    assert source.opens == 1


def test_stream_upload_stops_at_chunk_boundary_after_lease_loss() -> None:
    source = Source(b"ciphertext")
    blob = Blob("base/object")
    store = GCSRestartableStreamingObjectStore.from_bucket_client(cast(Any, Bucket(blob)))
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(RuntimeError, match="chunk boundary"):
        store.put_base_if_absent(
            source=source,
            object_name="base/object",
            metadata={"candidate": "sha"},
            cancelled=cancelled,
        )


def test_412_accepts_only_exact_existing_generation() -> None:
    source = Source(b"ciphertext")
    attempted = Blob("base/object", fail_precondition=True)
    existing = Blob("base/object")
    existing.value = source.value
    existing.metadata = {"candidate": "sha"}
    existing.reload()
    store = GCSRestartableStreamingObjectStore.from_bucket_client(
        cast(Any, Bucket(attempted, existing))
    )
    store.put_base_if_absent(
        source=source,
        object_name="base/object",
        metadata={"candidate": "sha"},
    )

    existing.crc32c = "different"
    with pytest.raises(PermanentObjectStoreError, match="differs"):
        store.put_base_if_absent(
            source=source,
            object_name="base/object",
            metadata={"candidate": "sha"},
        )
