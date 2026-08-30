"""COS adapter role tests: pinned restore download, retention inventory,
and the protected-manifest publisher."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from services.pitr.checksums import CRC32C, MD5, ObjectChecksum
from services.pitr.cos_inventory import CosRetentionInventoryReader
from services.pitr.cos_publish_store import CosProtectedManifestPublisher
from services.pitr.cos_restore_store import CosGenerationPinnedObjectReader
from services.pitr.object_store import PermanentObjectStoreError
from services.pitr.restore_manifest import RestoreObject
from tests.services.cos_test_support import FakeCos, cos_client_for

OBJECT = "ava-pitr/wal/00000001/000000010000000000000001.enc"


@pytest.fixture()
def fake() -> FakeCos:
    return FakeCos()


def make_reader(fake: FakeCos) -> CosGenerationPinnedObjectReader:
    return CosGenerationPinnedObjectReader.from_client(cos_client_for(fake))


def make_publisher(fake: FakeCos) -> CosProtectedManifestPublisher:
    return CosProtectedManifestPublisher.from_client(cos_client_for(fake))


def make_inventory(fake: FakeCos) -> CosRetentionInventoryReader:
    return CosRetentionInventoryReader.from_client(cos_client_for(fake), prefix="ava-pitr")


def _restore_object(payload: bytes, *, pin: str | None = None) -> RestoreObject:
    digest = hashlib.md5(payload).hexdigest()  # noqa: S324 — restore digest
    return RestoreObject(
        "000000010000000000000001",
        OBJECT,
        pin or digest,
        len(payload),
        MD5,
        digest,
        (("ava-key-id", "v1"),),
    )


# ── restore role ──


def test_download_exact_streams_and_verifies(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"restore-payload" * 8
    fake.seed(OBJECT, payload, {"ava-key-id": "v1"})
    reader = make_reader(fake)
    destination = tmp_path / "out.enc"

    reader.download_exact(_restore_object(payload), destination)

    assert destination.read_bytes() == payload
    assert not (tmp_path / ".out.enc.partial").exists()


def test_download_exact_rejects_tampered_content(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"restore-payload" * 8
    fake.seed(OBJECT, payload, {"ava-key-id": "v1"})
    fake.corrupt_get_keys.add(OBJECT)
    reader = make_reader(fake)
    destination = tmp_path / "out.enc"

    with pytest.raises(PermanentObjectStoreError, match="content differs"):
        reader.download_exact(_restore_object(payload), destination)

    assert not destination.exists()
    assert not (tmp_path / ".out.enc.partial").exists()


def test_download_exact_rejects_changed_pin(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"restore-payload" * 8
    fake.seed(OBJECT, payload, {"ava-key-id": "v1"})
    reader = make_reader(fake)

    with pytest.raises(PermanentObjectStoreError, match="changed"):
        reader.download_exact(_restore_object(payload, pin="0" * 32), tmp_path / "out.enc")


def test_download_exact_rejects_foreign_checksum_vocabulary(fake: FakeCos, tmp_path: Path) -> None:
    payload = b"payload"
    fake.seed(OBJECT, payload, {})
    reader = make_reader(fake)
    expected = RestoreObject(
        "n",
        OBJECT,
        hashlib.md5(payload).hexdigest(),  # noqa: S324 — fixture pin
        len(payload),
        CRC32C,
        "x",
        (),
    )

    with pytest.raises(PermanentObjectStoreError, match="not COS MD5"):
        reader.download_exact(expected, tmp_path / "out.enc")


# ── inventory role ──


def test_inventory_classifies_objects_and_flags_unknowns(fake: FakeCos) -> None:
    archive = "000000010000000000000001"
    rel = f"ava-pitr/wal/{archive[:8]}/{archive}.enc"
    fake.seed(rel, b"wal", {"ava-archive-name": archive})
    base_rel = "ava-pitr/base/20260830T043835Z/" + "a" * 64 + "/base.tar.zst.enc"
    fake.seed(base_rel, b"base", {})
    protected = "ava-pitr/protected/some-chain.json"
    fake.seed(protected, b"{}", {})
    orphan = "ava-pitr/wal/00000001/000000010000000000000002.enc"
    fake.seed(orphan, b"wal", {})
    foreign_ack = f"ava-pitr/wal/00000001/{archive}.enc.ack.json"
    fake.seed(foreign_ack, b"{}", {})

    snapshot = make_inventory(fake).snapshot()

    assert tuple(sorted(snapshot.unknown_names)) == (foreign_ack, orphan)
    by_kind = {item.kind: item for item in snapshot.objects}
    assert by_kind["wal"].archive_name == archive
    assert by_kind["wal"].checksum_algo == MD5
    assert by_kind["base"].archive_name is None
    assert all(item.object_name != protected for item in snapshot.objects)


# ── publisher role ──


def test_publish_manifest_puts_and_reads_back_bytes(
    fake: FakeCos,
) -> None:
    publisher = make_publisher(fake)
    payload = b'{"protected":true}'

    ack = publisher.put_manifest_if_absent(
        payload=payload,
        object_name="ava-pitr/protected/x.json",
        metadata={"ava-candidate-sha256": "s"},
    )

    digest = hashlib.md5(payload).hexdigest()  # noqa: S324 — manifest ACK digest
    assert ack.pin_token == digest
    assert ack.size == len(payload)
    assert ack.checksum == ObjectChecksum(MD5, digest)
    assert ack.created is True
    assert fake.objects["ava-pitr/protected/x.json"]["body"] == payload


def test_publish_manifest_adopts_identical_existing(fake: FakeCos) -> None:
    payload = b'{"protected":true}'
    fake.seed("ava-pitr/protected/x.json", payload, {"ava-candidate-sha256": "s"})
    publisher = make_publisher(fake)

    ack = publisher.put_manifest_if_absent(
        payload=payload,
        object_name="ava-pitr/protected/x.json",
        metadata={"ava-candidate-sha256": "s"},
    )

    assert ack.created is False
    assert ack.checksum == ObjectChecksum(MD5, hashlib.md5(payload).hexdigest())  # noqa: S324 — fixture digest


def test_publish_manifest_rejects_different_existing_payload(
    fake: FakeCos,
) -> None:
    fake.seed("ava-pitr/protected/x.json", b'{"protected":false}', {})
    publisher = make_publisher(fake)

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        publisher.put_manifest_if_absent(
            payload=b'{"protected":true}',
            object_name="ava-pitr/protected/x.json",
            metadata={},
        )


def test_publish_manifest_rejects_readback_mismatch(fake: FakeCos) -> None:
    fake.corrupt_get_keys.add("ava-pitr/protected/x.json")
    publisher = make_publisher(fake)

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        publisher.put_manifest_if_absent(
            payload=b'{"protected":true}',
            object_name="ava-pitr/protected/x.json",
            metadata={},
        )


def test_publish_manifest_rejects_multipart_etag(fake: FakeCos) -> None:
    payload = b'{"protected":true}'
    fake.seed("ava-pitr/protected/x.json", payload, {})
    fake.etag_overrides["ava-pitr/protected/x.json"] = "abc-4"
    publisher = make_publisher(fake)

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        publisher.put_manifest_if_absent(
            payload=payload,
            object_name="ava-pitr/protected/x.json",
            metadata={},
        )


def test_publish_manifest_rejects_existing_object_with_divergent_metadata(
    fake: FakeCos,
) -> None:
    payload = b'{"protected":true}'
    fake.seed("ava-pitr/protected/x.json", payload, {"ava-other": "z"})
    publisher = make_publisher(fake)

    with pytest.raises(PermanentObjectStoreError, match="differs"):
        publisher.put_manifest_if_absent(
            payload=payload,
            object_name="ava-pitr/protected/x.json",
            metadata={},
        )


def test_download_exact_rejects_same_size_bitflip(fake: FakeCos, tmp_path: Path) -> None:
    """A same-length corruption must trip the streamed MD5 check, not the
    size check (the size guard would otherwise mask the checksum branch)."""
    payload = b"restore-payload" * 8
    fake.seed(OBJECT, payload, {"ava-key-id": "v1"})
    fake.corrupt_bytes_keys.add(OBJECT)
    reader = make_reader(fake)
    destination = tmp_path / "out.enc"

    with pytest.raises(PermanentObjectStoreError, match="content differs"):
        reader.download_exact(_restore_object(payload), destination)

    assert not destination.exists()
    assert not (tmp_path / ".out.enc.partial").exists()
