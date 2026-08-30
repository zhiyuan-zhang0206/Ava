# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Aliyun OSS adapter role tests: object store, restartable streaming base
upload, pinned restore download, retention inventory, protected-manifest
publisher, the factory registration, and OSS-vocabulary manifest
normalization across the ACK / candidate / protected family."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.pitr.base_manifest import CandidateManifest
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)
from services.pitr.oss_store import OSSObjectStore
from services.pitr.restore_manifest import RestoreObject, required_archive_names
from services.pitr.store_factory import get_group_constructor_named
from services.pitr.uploader import ack_manifest_from_raw
from tests.services.oss_test_support import (
    PREFIX,
    WA_OBJECT,
    ChunkSource,
    FakeOssBucket,
    _md5,
    base_ack_metadata,
    make_inventory,
    make_publisher,
    make_reader,
    make_store,
    oss_credentials_file,
    wal_ack_metadata,
)

WAL_SOURCE = b"wal-ciphertext-payload" * 7
BASE_BYTES = bytes(range(256)) * 13  # 3328 bytes, non-aligned to 1024 parts

BASE_OBJECT = f"{PREFIX}/base/20260830T043835Z/{'a' * 64}/base.tar.zst.enc"

# Fixture identity for a multipart ETag. Built from a repeated character on
# purpose: a literal 32-hex string trips GitGuardian's high-entropy scanner
# even though it is a test fixture, never a real object identity.
OSS_ETAG_FIXTURE = "E" * 32 + "-3"


def _put_wal(store: OSSObjectStore, tmp_path: Path) -> RemoteObjectAck:
    path = tmp_path / "wal.enc"
    path.write_bytes(WAL_SOURCE)
    return store.put_wal_ciphertext_if_absent(path, WA_OBJECT, wal_ack_metadata())


# ── ObjectStore role ──


def test_put_wal_ciphertext_publishes_and_reobserves(tmp_path: Path) -> None:
    store = make_store(FakeOssBucket())
    ack = _put_wal(store, tmp_path)
    assert ack.object_name == WA_OBJECT
    assert ack.pin_token == _md5(WAL_SOURCE).upper()  # OSS ETag = content MD5 (server case)
    assert ack.checksum == ObjectChecksum(MD5, _md5(WAL_SOURCE))
    assert ack.size == len(WAL_SOURCE)
    assert ack.created is True
    assert dict(ack.metadata) == wal_ack_metadata()

    reobserved = store.stat(WA_OBJECT)
    assert reobserved is not None
    assert reobserved.pin_token == ack.pin_token
    assert reobserved.checksum == ack.checksum
    assert reobserved.size == ack.size
    assert dict(reobserved.metadata) == dict(ack.metadata)
    assert reobserved.created is False


def test_put_wal_ciphertext_adopts_identical_existing_object(tmp_path: Path) -> None:
    store = make_store(FakeOssBucket())
    first = _put_wal(store, tmp_path)
    second = _put_wal(store, tmp_path)
    assert second.created is False
    assert (second.pin_token, second.size, second.checksum) == (
        first.pin_token,
        first.size,
        first.checksum,
    )


def test_put_wal_ciphertext_rejects_different_content_under_same_name(
    tmp_path: Path,
) -> None:
    fake = FakeOssBucket()
    fake.seed(WA_OBJECT, data=b"different-content", metadata=wal_ack_metadata())
    store = make_store(fake)
    with pytest.raises(PermanentObjectStoreError, match="differs"):
        _put_wal(store, tmp_path)


# ── RestartableStreamingObjectStore role ──


def test_put_base_if_absent_streams_multipart_and_writes_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    fake = FakeOssBucket()
    store = make_store(fake)
    source = ChunkSource([BASE_BYTES[i : i + 900] for i in range(0, len(BASE_BYTES), 900)])
    metadata = base_ack_metadata(BASE_BYTES)
    ack = store.put_base_if_absent(source=source, object_name=BASE_OBJECT, metadata=metadata)
    assert ack.created is True
    assert ack.size == len(BASE_BYTES)
    assert ack.checksum == ObjectChecksum(MD5, _md5(BASE_BYTES))
    assert "-" in ack.pin_token  # multipart ETag carries the part count
    info = fake.files[BASE_OBJECT]
    assert info["data"] == BASE_BYTES
    assert info["metadata"] == metadata
    assert info["type"] == "Multipart"
    # Sidecar carries the exact ACK identity for the inventory.
    sidecar = store.read_sidecar(BASE_OBJECT)
    assert sidecar is not None
    assert sidecar["pin_token"] == ack.pin_token
    assert sidecar["size"] == ack.size
    assert sidecar["checksum_algo"] == MD5
    assert sidecar["checksum_value"] == ack.checksum.value
    assert sidecar["metadata"] == metadata


def test_put_base_adopts_after_complete_crash_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry whose complete hit FileAlreadyExists adopts the object exactly
    when its ETag matches our own part chain (the crash window between
    complete and sidecar publication)."""
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    store = make_store(FakeOssBucket())
    metadata = base_ack_metadata(BASE_BYTES)
    first = store.put_base_if_absent(
        source=ChunkSource([BASE_BYTES]), object_name=BASE_OBJECT, metadata=metadata
    )
    second = store.put_base_if_absent(
        source=ChunkSource([BASE_BYTES]), object_name=BASE_OBJECT, metadata=metadata
    )
    assert second.created is False
    assert (second.pin_token, second.size, second.checksum) == (
        first.pin_token,
        first.size,
        first.checksum,
    )


def test_put_base_rejects_different_content_under_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    fake = FakeOssBucket()
    store = make_store(fake)
    metadata = base_ack_metadata(BASE_BYTES)
    store.put_base_if_absent(
        source=ChunkSource([BASE_BYTES]), object_name=BASE_OBJECT, metadata=metadata
    )
    with pytest.raises(PermanentObjectStoreError, match="differs"):
        store.put_base_if_absent(
            source=ChunkSource([BASE_BYTES + b"x"]),
            object_name=BASE_OBJECT,
            metadata=metadata,
        )


def test_put_base_surfaces_cancellation_before_publication() -> None:
    store = make_store(FakeOssBucket())
    with pytest.raises(RuntimeError, match="cancelled"):
        store.put_base_if_absent(
            source=ChunkSource([BASE_BYTES]),
            object_name=BASE_OBJECT,
            metadata=base_ack_metadata(BASE_BYTES),
            cancelled=lambda: True,
        )


def test_stat_resolves_multipart_base_identity_through_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    store = make_store(FakeOssBucket())
    metadata = base_ack_metadata(BASE_BYTES)
    ack = store.put_base_if_absent(
        source=ChunkSource([BASE_BYTES]), object_name=BASE_OBJECT, metadata=metadata
    )
    reobserved = store.stat(BASE_OBJECT)
    assert reobserved is not None
    assert reobserved.pin_token == ack.pin_token
    assert reobserved.checksum == ObjectChecksum(MD5, _md5(BASE_BYTES))


# ── viewer pinned restore ──


def _restore_object(
    *,
    object_name: str = WA_OBJECT,
    pin_token: str | None = None,
    payload: bytes = WAL_SOURCE,
    checksum_value: str | None = None,
    metadata: dict[str, str] | None = None,
) -> RestoreObject:
    return RestoreObject(
        "000000010000000000000001",
        object_name,
        pin_token or _md5(payload).upper(),
        len(payload),
        MD5,
        checksum_value or _md5(payload),
        tuple(sorted((metadata or wal_ack_metadata()).items())),
    )


def test_download_exact_streams_and_verifies(tmp_path: Path) -> None:
    fake = FakeOssBucket()
    fake.seed(WA_OBJECT, data=WAL_SOURCE, metadata=wal_ack_metadata())
    reader = make_reader(fake)
    destination = tmp_path / "w" / "restored.enc"
    reader.download_exact(_restore_object(), destination)
    assert destination.read_bytes() == WAL_SOURCE
    assert not list(destination.parent.glob(".restored.enc.partial"))


def test_download_exact_rejects_tampered_content(tmp_path: Path) -> None:
    fake = FakeOssBucket()
    fake.seed(WA_OBJECT, data=WAL_SOURCE + b"x", metadata=wal_ack_metadata())
    reader = make_reader(fake)
    with pytest.raises(
        PermanentObjectStoreError,
        match=r"does not exist or differs|content differs|properties differ",
    ):
        reader.download_exact(_restore_object(), tmp_path / "restored.enc")


def test_download_exact_rejects_mismatched_pin_token(tmp_path: Path) -> None:
    fake = FakeOssBucket()
    fake.seed(WA_OBJECT, data=WAL_SOURCE, metadata=wal_ack_metadata())
    reader = make_reader(fake)
    with pytest.raises(PermanentObjectStoreError, match="does not exist or differs"):
        reader.download_exact(_restore_object(pin_token="0" * 32), tmp_path / "restored.enc")


def test_download_exact_rejects_foreign_checksum_algo(tmp_path: Path) -> None:
    from services.pitr.checksums import CRC32C

    fake = FakeOssBucket()
    fake.seed(WA_OBJECT, data=WAL_SOURCE, metadata=wal_ack_metadata())
    reader = make_reader(fake)
    expected = RestoreObject(
        "000000010000000000000001",
        WA_OBJECT,
        _md5(WAL_SOURCE).upper(),
        len(WAL_SOURCE),
        CRC32C,
        "crc32c-value",
        tuple(sorted(wal_ack_metadata().items())),
    )
    with pytest.raises(PermanentObjectStoreError, match="not Aliyun OSS MD5"):
        reader.download_exact(expected, tmp_path / "restored.enc")


# ── retention inventory ──


def _seed_inventory(fake: FakeOssBucket) -> None:
    base = BASE_OBJECT
    store = make_store(fake)
    base_payload = b"base-ciphertext"
    base_meta = base_ack_metadata(base_payload)
    store.put_base_if_absent(
        source=ChunkSource([base_payload]), object_name=base, metadata=base_meta
    )
    fake.seed(WA_OBJECT, data=WAL_SOURCE, metadata=wal_ack_metadata())
    fake.seed(
        f"{PREFIX}/protected/chain-123.json",
        data=b"{}",
        metadata={"ava-protected": "true"},
    )
    fake.seed(f"{PREFIX}/junk/whatever.json", data=b"x")


def test_inventory_classifies_base_and_wal_and_flags_unknowns() -> None:
    fake = FakeOssBucket()
    _seed_inventory(fake)
    inventory = make_inventory(fake).snapshot()
    names = [item.object_name for item in inventory.objects]
    assert BASE_OBJECT in names
    assert WA_OBJECT in names
    assert {item.checksum_algo for item in inventory.objects} == {MD5}
    assert inventory.unknown_names == (f"{PREFIX}/junk/whatever.json",)


def test_inventory_rejects_unverifiable_base_without_sidecar() -> None:
    fake = FakeOssBucket()
    fake.seed(BASE_OBJECT, data=b"base", metadata=base_ack_metadata(b"base"), multipart=True)
    inventory = make_inventory(fake).snapshot()
    assert inventory.objects == ()
    assert inventory.unknown_names == (BASE_OBJECT,)


# ── protected manifest publisher ──


def test_publish_manifest_puts_and_verifies() -> None:
    fake = FakeOssBucket()
    publisher = make_publisher(fake)
    payload = b'{"protected": true}'
    metadata = {"ava-chain-id": "chain-1", "ava-protected": "true"}
    ack = publisher.put_manifest_if_absent(
        payload=payload, object_name=f"{PREFIX}/protected/chain-1.json", metadata=metadata
    )
    assert ack.created is True
    assert ack.size == len(payload)
    assert ack.checksum == ObjectChecksum(MD5, _md5(payload))
    assert dict(ack.metadata) == metadata
    assert fake.files[f"{PREFIX}/protected/chain-1.json"]["data"] == payload


def test_publish_manifest_rejects_different_existing_bytes() -> None:
    fake = FakeOssBucket()
    name = f"{PREFIX}/protected/chain-1.json"
    fake.seed(name, data=b"old", metadata={"ava-chain-id": "chain-1"})
    publisher = make_publisher(fake)
    with pytest.raises(PermanentObjectStoreError, match="differs"):
        publisher.put_manifest_if_absent(
            payload=b'{"protected": true}', object_name=name, metadata={"ava-chain-id": "chain-1"}
        )


# ── fail-closed identity assertions (M1 / M2) and error taxonomy (M3) ──


def test_part_etag_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    fake = FakeOssBucket()
    fake.corrupt_part_etags = True
    store = make_store(fake)
    with pytest.raises(PermanentObjectStoreError, match="ETag does not match"):
        store.put_base_if_absent(
            source=ChunkSource([BASE_BYTES]),
            object_name=BASE_OBJECT,
            metadata=base_ack_metadata(BASE_BYTES),
        )


def test_complete_etag_chain_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.pitr.oss_store.PART_SIZE", 1024)
    fake = FakeOssBucket()
    fake.corrupt_complete_etag = True
    store = make_store(fake)
    with pytest.raises(PermanentObjectStoreError, match="ETag does not match"):
        store.put_base_if_absent(
            source=ChunkSource([BASE_BYTES]),
            object_name=BASE_OBJECT,
            metadata=base_ack_metadata(BASE_BYTES),
        )


def test_transient_server_errors_map_to_transient() -> None:
    fake = FakeOssBucket()
    fake.head_error = (503, "ServiceUnavailable")
    store = make_store(fake)
    with pytest.raises(TransientObjectStoreError):
        store.stat(WA_OBJECT)


def test_transport_errors_map_to_transient() -> None:
    fake = FakeOssBucket()
    fake.request_error = True
    store = make_store(fake)
    with pytest.raises(TransientObjectStoreError):
        store.stat(WA_OBJECT)


def test_permanent_errors_map_to_permanent() -> None:
    fake = FakeOssBucket()
    fake.head_error = (403, "AccessDenied")
    store = make_store(fake)
    with pytest.raises(PermanentObjectStoreError):
        store.stat(WA_OBJECT)


def test_read_sidecar_rejects_tampered_bytes() -> None:
    fake = FakeOssBucket()
    store = make_store(fake)
    metadata = base_ack_metadata(BASE_BYTES)
    store.put_base_if_absent(
        source=ChunkSource([BASE_BYTES]), object_name=BASE_OBJECT, metadata=metadata
    )
    # Tamper the sidecar bytes without touching its ETag — the reader must
    # refuse the mismatched identity instead of serving a wrong checksum.
    sidecar_name = f"{BASE_OBJECT}.ack.json"
    record = fake.files[sidecar_name]
    data = record["data"]
    marker = b'"checksum_value":"'
    offset = data.index(marker) + len(marker)
    flipped = b"0" if data[offset : offset + 1] != b"0" else b"1"
    record["data"] = data[:offset] + flipped + data[offset + 1 :]
    with pytest.raises(PermanentObjectStoreError, match="sidecar differs from its ETag"):
        store.read_sidecar(BASE_OBJECT)


def test_restore_worker_input_builds_oss_store_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_operation_runtime as restore_runtime
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "oss")
    monkeypatch.setattr(settings.physical_backup, "pitr_restore_proof_enabled", True)
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    monkeypatch.setattr(
        settings.physical_backup, "pitr_oss_endpoint", "https://oss-cn-shanghai.aliyuncs.com"
    )
    monkeypatch.setattr(settings.physical_backup, "pitr_oss_bucket", "ava-pitr-store")
    monkeypatch.setattr(
        settings.physical_backup,
        "pitr_oss_viewer_credentials_file",
        tmp_path / "viewer.json",
    )
    monkeypatch.setattr(restore_runtime, "direct_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(restore_runtime, "live_data_directory", lambda: "/live/data")
    monkeypatch.setattr(restore_runtime, "pg_tool", lambda _name: Path("/usr/bin/true"))  # type: ignore[no-untyped-call]

    inputs = restore_runtime.input_for(_oss_candidate())
    assert inputs.backend == "oss"
    assert set(dict(inputs.store_args)) == {
        "endpoint",
        "bucket",
        "prefix",
        "viewer_credentials_file",
    }
    assert dict(inputs.store_args)["viewer_credentials_file"] == str(tmp_path / "viewer.json")


def _oss_candidate() -> CandidateManifest:
    from services.pitr.base_manifest import BaseObject, WalRange

    return CandidateManifest(
        schema_version=1,
        chain_id="activation-20260830T043835Z-c1cfa2ee",
        protected=False,
        postgres_major=17,
        database_name="ava_main",
        system_identifier="7656686487711429617",
        wal_segment_size=16777216,
        timeline=1,
        start_lsn="A4/7FC179B0",
        end_lsn="A4/89EC6820",
        wal_ranges=(WalRange(1, "A4/7FC179B0", "A4/89EC6820"),),
        base_object=BaseObject(
            object_name=BASE_OBJECT,
            pin_token=OSS_ETAG_FIXTURE,
            ciphertext_size=len(BASE_BYTES),
            ciphertext_crc32c="crc-local",
            ciphertext_checksum_algo=MD5,
            ciphertext_checksum_value=_md5(BASE_BYTES),
            source_sha256="a" * 64,
            source_size=1234,
            key_id="prod-v1",
            encryption_format="AVAPITR1",
        ),
        native_manifest_sha256="d" * 64,
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name=BASE_OBJECT,
        native_manifest_container_pin_token=OSS_ETAG_FIXTURE,
        migration_set_sha256="e" * 64,
    )


# ── factory registration ──


def test_factory_constructs_every_role_for_the_oss_backend(tmp_path: Path) -> None:
    credentials = oss_credentials_file(tmp_path)
    viewer = tmp_path / "viewer.json"
    viewer.write_text(json.dumps({"access_key_id": "viewer-ak", "access_key_secret": "s"}))
    viewer.chmod(0o600)
    group = get_group_constructor_named("oss")(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="ava-pitr",
        prefix=PREFIX,
        credentials_file=credentials,
        viewer_credentials_file=viewer,
    )
    assert group.object_store() is not None
    assert group.restartable_streaming_object_store() is not None
    assert group.viewer_object_store() is not None
    assert group.generation_pinned_object_reader() is not None
    assert group.retention_inventory_reader() is not None
    assert group.protected_manifest_publisher() is not None


# ── OSS-vocabulary manifest normalization (ACK / candidate / protected) ──


def _oss_ack_raw() -> dict[str, Any]:
    return {
        "archive_name": "000000010000000000000001",
        "source_sha256": "a" * 64,
        "source_size": 16,
        "object_name": WA_OBJECT,
        "pin_token": _md5(WAL_SOURCE).upper(),
        "ciphertext_size": len(WAL_SOURCE),
        "ciphertext_crc32c": "crc-local",
        "ciphertext_checksum_algo": MD5,
        "ciphertext_checksum_value": _md5(WAL_SOURCE),
        "encryption_format": "AVAPITR1",
        "key_id": "prod-v1",
        "acknowledged_at": "2026-08-30T10:00:00+00:00",
    }


def test_oss_ack_normalizes_through_ack_and_restore_objects(tmp_path: Path) -> None:
    from services.pitr.restore_manifest import wal_objects_from_acks

    ack_dir = tmp_path / "ack"
    ack_dir.mkdir()
    (ack_dir / "000000010000000000000001.ack.json").write_text(json.dumps(_oss_ack_raw()))
    ack = ack_manifest_from_raw(
        json.loads((ack_dir / "000000010000000000000001.ack.json").read_text())
    )
    assert ack.pin_token == _md5(WAL_SOURCE).upper()
    assert ack.ciphertext_checksum_algo == MD5
    assert ack.ciphertext_checksum_value == _md5(WAL_SOURCE)
    objects = wal_objects_from_acks(ack_dir=ack_dir, archive_names=("000000010000000000000001",))
    assert objects[0].pin_token == ack.pin_token
    assert objects[0].checksum_algo == MD5
    assert objects[0].checksum_value == _md5(WAL_SOURCE)


def test_oss_protected_manifest_round_trips_stable() -> None:
    from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
    from services.pitr.restore_manifest import ProtectedManifest, candidate_sha256
    from services.pitr.restore_manifest import RestoreProof as _Proof

    candidate = CandidateManifest(
        schema_version=1,
        chain_id="activation-20260830T043835Z-c1cfa2ee",
        protected=False,
        postgres_major=17,
        database_name="ava_main",
        system_identifier="7656686487711429617",
        wal_segment_size=16777216,
        timeline=1,
        start_lsn="A4/7FC179B0",
        end_lsn="A4/89EC6820",
        wal_ranges=(WalRange(1, "A4/7FC179B0", "A4/89EC6820"),),
        base_object=BaseObject(
            object_name=BASE_OBJECT,
            pin_token=OSS_ETAG_FIXTURE,
            ciphertext_size=len(BASE_BYTES),
            ciphertext_crc32c="crc-local",
            ciphertext_checksum_algo=MD5,
            ciphertext_checksum_value=_md5(BASE_BYTES),
            source_sha256="a" * 64,
            source_size=1234,
            key_id="prod-v1",
            encryption_format="AVAPITR1",
        ),
        native_manifest_sha256="d" * 64,
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name=BASE_OBJECT,
        native_manifest_container_pin_token=OSS_ETAG_FIXTURE,
        migration_set_sha256="e" * 64,
    )
    names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    proof = _Proof(
        run_id="run",
        started_at="2026-08-30T03:00:00+00:00",
        completed_at="2026-08-30T03:01:00+00:00",
        target_lsn=candidate.end_lsn,
        achieved_lsn=candidate.end_lsn,
        live_postgres_pid=1,
        live_probe_sha256="live",
        candidate_verify_evidence_sha256="verify",
        replay_seconds=2.0,
        smoke_seconds=3.0,
        restored_verify_seconds=4.0,
        downloaded_bytes=100,
        restored_fingerprint_sha256="restored",
    )
    protected = ProtectedManifest(
        schema_version=1,
        protected=True,
        chain_id=candidate.chain_id,
        candidate_sha256=candidate_sha256(candidate),
        candidate=candidate,
        base=RestoreObject(
            "base.tar.zst.enc",
            BASE_OBJECT,
            candidate.base_object.pin_token,
            candidate.base_object.ciphertext_size,
            MD5,
            candidate.base_object.ciphertext_checksum_value,
            tuple(sorted(base_ack_metadata(BASE_BYTES).items())),
        ),
        wal=tuple(
            RestoreObject(
                name,
                f"{PREFIX}/wal/{name[:8]}/{name}.enc",
                "Y" * 32,
                10,
                MD5,
                "z" * 32,
                (),
            )
            for name in names
        ),
        target_lsn=candidate.end_lsn,
        wal_segment_size=candidate.wal_segment_size,
        proof=proof,
    )
    # The OSS-shaped identities survive the canonical serialization round trip.
    reparsed = ProtectedManifest.from_json(protected.to_json())
    assert reparsed == protected
    assert reparsed.base.pin_token == OSS_ETAG_FIXTURE
    assert reparsed.base.checksum_algo == MD5
    assert reparsed.base.checksum_value == _md5(BASE_BYTES)
    assert all(item.checksum_algo == MD5 for item in reparsed.wal)
    # And the embedded candidate parses from the same bytes.
    assert reparsed.candidate == candidate
