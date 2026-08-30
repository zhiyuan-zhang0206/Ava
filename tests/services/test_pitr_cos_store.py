"""COS adapter tests: the ObjectStore + restartable streaming roles."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.cos_store import CosObjectStore
from services.pitr.object_store import (
    PermanentObjectStoreError,
    TransientObjectStoreError,
)
from tests.services.cos_test_support import FakeCos, cos_client_for

OBJECT = "ava-pitr/wal/00000001/000000010000000000000001.enc"


class ChunkSource:
    """Deterministic restartable ciphertext for streaming-role tests."""

    def __init__(self, chunks: list[bytes], *, size: int | None = None) -> None:
        self._chunks = chunks
        self._size = size if size is not None else sum(len(chunk) for chunk in chunks)
        self.walks = 0

    @property
    def ciphertext_size(self) -> int:
        return self._size

    @property
    def ciphertext_crc32c(self) -> str:
        return ""

    def iter_chunks(self) -> Iterator[bytes]:
        self.walks += 1
        yield from self._chunks


def make_store(fake: FakeCos) -> CosObjectStore:
    return CosObjectStore.from_client(cos_client_for(fake))


METADATA = {
    "ava-archive-name": "000000010000000000000001",
    "ava-key-id": "v1",
}


@pytest.fixture()
def fake() -> FakeCos:
    return FakeCos()


# ── WAL publish ──


def test_put_wal_publishes_and_verifies_identity(fake: FakeCos, tmp_path: Path) -> None:
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(b"ciphertext-payload")
    metadata = dict(METADATA)

    ack = store.put_wal_ciphertext_if_absent(staged, OBJECT, metadata)

    import hashlib

    digest = hashlib.md5(b"ciphertext-payload").hexdigest()  # noqa: S324 — fixture digest
    assert ack.object_name == OBJECT
    assert ack.pin_token == digest
    assert ack.size == len(b"ciphertext-payload")
    assert ack.checksum == ObjectChecksum(MD5, digest)
    assert ack.metadata == metadata
    assert ack.created is True
    assert fake.objects[OBJECT]["body"] == b"ciphertext-payload"


def test_put_wal_adopts_identical_existing_object(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"same-content"
    digest = fake.seed(OBJECT, payload, dict(METADATA))
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))

    assert ack.pin_token == digest
    assert ack.created is False
    assert ack.checksum == ObjectChecksum(MD5, digest)


def test_put_wal_rejects_different_content_under_same_name(fake: FakeCos, tmp_path: Path) -> None:
    fake.seed(OBJECT, b"other-content", dict(METADATA))
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(b"local-content")

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))


def test_put_wal_rejects_different_metadata_under_same_name(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"same-content"
    fake.seed(OBJECT, payload, {**METADATA, "ava-key-id": "v2"})
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(payload)

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))


def test_put_wal_maps_precondition_race_to_transient(fake: FakeCos, tmp_path: Path) -> None:
    fake.precondition_race_keys.add(OBJECT)
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(b"content")

    with pytest.raises(TransientObjectStoreError, match="raced"):
        store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))


def test_put_wal_ceiling_fails_permanent_before_any_request(
    fake: FakeCos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(CosObjectStore, "SIMPLE_PUT_LIMIT_BYTES", 64)
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(b"x" * 128)

    with pytest.raises(PermanentObjectStoreError, match="simple-PUT limit"):
        store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))
    assert fake.objects == {}


def test_put_wal_maps_rejected_request_to_permanent(fake: FakeCos, tmp_path: Path) -> None:
    fake.reject_put_keys.add(OBJECT)
    store = make_store(fake)
    staged = tmp_path / "wal.enc"
    staged.write_bytes(b"content")

    with pytest.raises(PermanentObjectStoreError, match="rejected"):
        store.put_wal_ciphertext_if_absent(staged, OBJECT, dict(METADATA))


# ── base streaming publish ──


def test_put_base_streams_and_verifies_identity(fake: FakeCos) -> None:
    store = make_store(fake)
    source = ChunkSource([b"hello ", b"world"])
    metadata = {"ava-candidate-sha256": "c" * 64}

    ack = store.put_base_if_absent(source=source, object_name=OBJECT, metadata=metadata)

    import hashlib

    digest = hashlib.md5(b"hello world").hexdigest()  # noqa: S324 — fixture digest
    assert ack.pin_token == digest
    assert ack.checksum == ObjectChecksum(MD5, digest)
    assert ack.size == 11
    assert ack.created is True
    assert fake.objects[OBJECT]["body"] == b"hello world"
    assert source.walks == 2


def test_put_base_cancelled_at_chunk_boundary_leaves_nothing(fake: FakeCos) -> None:
    store = make_store(fake)
    source = ChunkSource([b"hello ", b"world"])
    checks = {"count": 0}

    def cancelled() -> bool:
        checks["count"] += 1
        return checks["count"] > 4

    with pytest.raises(RuntimeError, match="chunk boundary"):
        store.put_base_if_absent(
            source=source,
            object_name=OBJECT,
            metadata={"candidate": "sha"},
            cancelled=cancelled,
        )
    assert OBJECT not in fake.objects


def test_put_base_rejects_size_mismatch(fake: FakeCos) -> None:
    store = make_store(fake)
    source = ChunkSource([b"hello "], size=100)

    with pytest.raises(PermanentObjectStoreError, match="size differs"):
        store.put_base_if_absent(source=source, object_name=OBJECT, metadata={"candidate": "sha"})


# ── stat ──


def test_stat_returns_identity_for_published_object(fake: FakeCos) -> None:
    payload = b"stored"
    digest = fake.seed(OBJECT, payload, dict(METADATA))
    store = make_store(fake)

    ack = store.stat(OBJECT)

    assert ack is not None
    assert ack.pin_token == digest
    assert ack.size == len(payload)
    assert ack.checksum == ObjectChecksum(MD5, digest)
    assert ack.metadata == METADATA
    assert ack.created is False


def test_stat_returns_none_for_missing_object(fake: FakeCos) -> None:
    store = make_store(fake)
    assert store.stat(OBJECT) is None


def test_stat_rejects_multipart_etag(fake: FakeCos) -> None:
    payload = b"x"
    fake.seed(OBJECT, payload, {})
    fake.etag_overrides[OBJECT] = "a1b2c3d4-3"
    store = make_store(fake)

    with pytest.raises(PermanentObjectStoreError, match="multipart ETag"):
        store.stat(OBJECT)


# ── manifest-family ACK normalization (ACK -> candidate -> protected) ──


def test_cos_ack_normalizes_into_candidate_and_protected_manifests(
    fake: FakeCos, tmp_path: Path
) -> None:
    """One COS ACK (ETag pin + MD5 digest) must stay first-class through the
    whole manifest family: ACK manifest, candidate manifest, and the
    protected restore objects that pin the same identity."""
    import hashlib
    import json

    from services.pitr.base_manifest import CandidateManifest, base_object_from_ack
    from services.pitr.restore_manifest import (
        ProtectedManifest,
        RestoreObject,
        required_archive_names,
    )
    from services.pitr.uploader import AckManifest, ack_manifest_from_raw
    from tests.services.test_pitr_store_contract import (
        _LEGACY_CANDIDATE_JSON,
        RestoreProofFixture,
        candidate_sha256_of,
    )

    payload = b"ciphertext-bytes"
    store = make_store(fake)
    metadata = {"ava-canary": "y"}

    ack = store.put_base_if_absent(
        source=ChunkSource([payload]), object_name=OBJECT, metadata=metadata
    )
    digest = ack.pin_token
    assert digest == hashlib.md5(payload).hexdigest()  # noqa: S324 — fixture digest

    # ACK manifest: COS vocabulary round-trips through the durable JSON.
    raw_ack = {
        "archive_name": "000000010000000000000001",
        "source_sha256": "a" * 64,
        "source_size": 7,
        "object_name": OBJECT,
        "pin_token": digest,
        "ciphertext_size": len(payload),
        "ciphertext_crc32c": "local-crc32c",
        "ciphertext_checksum_algo": MD5,
        "ciphertext_checksum_value": digest,
        "encryption_format": "AVAPITR1",
        "key_id": "v1",
        "acknowledged_at": "2026-08-30T10:00:00+00:00",
    }
    parsed = ack_manifest_from_raw(raw_ack)
    assert isinstance(parsed, AckManifest)
    assert parsed.pin_token == digest
    assert parsed.ciphertext_checksum_algo == MD5
    assert parsed.ciphertext_checksum_value == digest
    assert parsed.ciphertext_crc32c == "local-crc32c"

    # Candidate: the base-object port of the ACK keeps the COS identity.
    base_object = base_object_from_ack(
        ack,
        ciphertext_crc32c="local-crc32c",
        source_sha256="b" * 64,
        source_size=7,
        key_id="v1",
        encryption_format="AVAPITRB1",
    )
    assert base_object.pin_token == digest
    assert base_object.ciphertext_checksum_algo == MD5
    assert base_object.ciphertext_checksum_value == digest
    assert base_object.ciphertext_crc32c == "local-crc32c"

    # Protected: an embedded COS-vocabulary candidate + MD5 restore objects
    # parse and re-serialize without drifting out of the vocabulary.
    candidate_raw = json.loads(_LEGACY_CANDIDATE_JSON)
    candidate_raw["base_object"].pop("generation", None)
    candidate_raw["base_object"].update(
        {
            "pin_token": digest,
            "ciphertext_checksum_algo": MD5,
            "ciphertext_checksum_value": digest,
            "ciphertext_crc32c": "local-crc32c",
        }
    )
    candidate_raw["native_manifest_container_pin_token"] = digest
    candidate_raw.pop("native_manifest_container_generation", None)
    candidate = CandidateManifest.from_json(json.dumps(candidate_raw))
    names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    protected_raw: dict[str, object] = {
        "schema_version": 1,
        "protected": True,
        "chain_id": candidate.chain_id,
        "candidate_sha256": candidate_sha256_of(candidate),
        "candidate": json.loads(candidate.to_json()),
        "base": {
            "archive_name": "base.tar.zst.enc",
            "object_name": candidate.base_object.object_name,
            "pin_token": digest,
            "size": candidate.base_object.ciphertext_size,
            "checksum_algo": MD5,
            "checksum_value": digest,
            "metadata": [],
        },
        "wal": [
            {
                "archive_name": name,
                "object_name": f"ava-pitr/wal/{name[:8]}/{name}.enc",
                "pin_token": digest,
                "size": 10,
                "checksum_algo": MD5,
                "checksum_value": digest,
                "metadata": [["ava-key-id", "v1"]],
            }
            for name in names
        ],
        "target_lsn": candidate.end_lsn,
        "wal_segment_size": candidate.wal_segment_size,
        "proof": RestoreProofFixture().__dict__,
    }
    protected = ProtectedManifest.from_json(json.dumps(protected_raw))
    assert protected.base.pin_token == digest
    assert protected.base.checksum_algo == MD5
    assert protected.base.checksum_value == digest
    assert protected.candidate.base_object.pin_token == digest
    assert all(item.checksum_algo == MD5 for item in protected.wal)
    assert ProtectedManifest.from_json(protected.to_json()) == protected
    # The COS vocabulary does not masquerade as the GCS one.
    assert (
        RestoreObject("n", OBJECT, digest, 10, MD5, digest, (("ava-key-id", "v1"),)).checksum_algo
        == MD5
    )
