from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, cast

import google_crc32c
import pytest

from services.pitr.object_store import PermanentObjectStoreError
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_object_store import (
    GCSGenerationPinnedObjectReader,
    _ReadableBucket,
)


def _crc32c(value: bytes) -> str:
    checksum = google_crc32c.Checksum()
    checksum.update(value)  # pyright: ignore[reportUnknownMemberType]
    return base64.b64encode(checksum.digest()).decode("ascii")


def _expected(value: bytes = b"generation-pinned ciphertext") -> RestoreObject:
    return RestoreObject(
        archive_name="000000010000000000000001",
        object_name="ava-pitr/wal/00000001/000000010000000000000001.enc",
        pin_token="17",  # noqa: S106 — test fixture
        size=len(value),
        checksum_algo="crc32c",
        checksum_value=_crc32c(value),
        metadata=(("ava-key-id", "prod-v1"), ("ava-source-size", "16")),
    )


class _Blob:
    def __init__(
        self,
        expected: RestoreObject,
        value: bytes,
        *,
        before_download: Callable[[], None] | None = None,
    ) -> None:
        self.name = expected.object_name
        self.generation: int | str | None = int(expected.pin_token)
        self.size: int | str | None = expected.size
        self.crc32c: str | None = expected.checksum_value
        self.metadata: Mapping[str, str] | None = dict(expected.metadata)
        self.value = value
        self.before_download = before_download
        self.download_kwargs: dict[str, object] | None = None

    def download_to_file(self, file_obj: BinaryIO, **kwargs: object) -> None:
        self.download_kwargs = kwargs
        if self.before_download is not None:
            self.before_download()
        file_obj.write(self.value)


class _Bucket:
    def __init__(
        self,
        blob: _Blob | None,
        *,
        before_get: Callable[[], None] | None = None,
    ) -> None:
        self.blob = blob
        self.before_get = before_get
        self.get_name: str | None = None
        self.get_kwargs: dict[str, object] | None = None

    def get_blob(self, name: str, **kwargs: object) -> _Blob | None:
        self.get_name = name
        self.get_kwargs = kwargs
        if self.before_get is not None:
            self.before_get()
        return self.blob


def _reader(bucket: _Bucket) -> GCSGenerationPinnedObjectReader:
    return GCSGenerationPinnedObjectReader.from_bucket_client(cast(_ReadableBucket, bucket))


def test_download_pins_lookup_and_read_to_the_exact_generation(tmp_path: Path) -> None:
    value = b"generation-pinned ciphertext"
    expected = _expected(value)
    blob = _Blob(expected, value)
    bucket = _Bucket(blob)
    destination = tmp_path / "archive" / expected.archive_name

    _reader(bucket).download_exact(expected, destination)

    assert destination.read_bytes() == value
    assert bucket.get_name == expected.object_name
    assert bucket.get_kwargs is not None
    assert bucket.get_kwargs["generation"] == int(expected.pin_token)
    assert blob.download_kwargs is not None
    assert blob.download_kwargs["if_generation_match"] == int(expected.pin_token)
    assert blob.download_kwargs["checksum"] == "crc32c"
    assert not (destination.parent / f".{destination.name}.partial").exists()


@pytest.mark.parametrize(
    ("field", "different"),
    [
        ("name", "another-object"),
        ("generation", 18),
        ("size", 1),
        ("crc32c", "different-crc"),
        ("metadata", {"ava-key-id": "another-key"}),
    ],
)
def test_download_rejects_remote_property_mismatch_before_writing(
    tmp_path: Path, field: str, different: object
) -> None:
    expected = _expected()
    blob = _Blob(expected, b"generation-pinned ciphertext")
    setattr(blob, field, different)
    destination = tmp_path / expected.archive_name

    with pytest.raises(PermanentObjectStoreError, match="properties differ"):
        _reader(_Bucket(blob)).download_exact(expected, destination)

    assert not destination.exists()
    assert not (tmp_path / f".{destination.name}.partial").exists()
    assert blob.download_kwargs is None


def test_non_gcs_pin_token_or_digest_fails_closed_before_lookup(
    tmp_path: Path,
) -> None:
    expected = _expected()
    blob = _Blob(expected, b"generation-pinned ciphertext")
    bucket = _Bucket(blob)
    reader = _reader(bucket)

    from dataclasses import replace

    foreign_pin = replace(expected, pin_token="fs123:md5")  # noqa: S106 — test fixture
    with pytest.raises(PermanentObjectStoreError, match="not a GCS generation"):
        reader.download_exact(foreign_pin, tmp_path / "out")
    foreign_algo = replace(expected, checksum_algo="md5")
    with pytest.raises(PermanentObjectStoreError, match="not GCS CRC32C"):
        reader.download_exact(foreign_algo, tmp_path / "out")
    assert bucket.get_name is None


def test_download_rejects_short_content_and_removes_partial(tmp_path: Path) -> None:
    expected = _expected()
    blob = _Blob(expected, b"short")
    destination = tmp_path / expected.archive_name

    with pytest.raises(PermanentObjectStoreError, match="content differs"):
        _reader(_Bucket(blob)).download_exact(expected, destination)

    assert not destination.exists()
    assert not (tmp_path / f".{destination.name}.partial").exists()


def test_partial_creation_is_exclusive_against_a_check_to_open_race(tmp_path: Path) -> None:
    expected = _expected()
    destination = tmp_path / expected.archive_name
    partial = tmp_path / f".{destination.name}.partial"

    def race_partial() -> None:
        partial.write_bytes(b"other owner")

    blob = _Blob(expected, b"generation-pinned ciphertext")
    with pytest.raises(FileExistsError):
        _reader(_Bucket(blob, before_get=race_partial)).download_exact(expected, destination)

    assert partial.read_bytes() == b"other owner"
    assert not destination.exists()


def test_destination_publication_is_exclusive_against_a_download_race(tmp_path: Path) -> None:
    expected = _expected()
    destination = tmp_path / expected.archive_name

    def race_destination() -> None:
        destination.write_bytes(b"other owner")

    blob = _Blob(
        expected,
        b"generation-pinned ciphertext",
        before_download=race_destination,
    )

    with pytest.raises(FileExistsError):
        _reader(_Bucket(blob)).download_exact(expected, destination)

    assert destination.read_bytes() == b"other owner"
    assert not (tmp_path / f".{destination.name}.partial").exists()
