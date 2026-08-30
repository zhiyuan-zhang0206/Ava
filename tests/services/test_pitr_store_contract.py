"""Contract tests for the PITR store abstraction (PR-A).

These lock the hard gates of the abstraction itself, independent of the
GCS adapter behavior (which the existing per-adapter tests cover):

- the ACK carries a backend-owned pin token plus an (algo, value) digest,
  and legacy on-disk ACKs normalize without ambiguity;
- checksum dispatch never compares across algorithm vocabularies;
- the factory fails fast on unknown backends and never falls back;
- the token-manager skeleton round-trips and reports health honestly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.pitr.checksums import (
    CRC32C,
    KNOWN_CHECKSUM_ALGOS,
    MD5,
    ObjectChecksum,
    digest_bytes,
    matches,
)
from services.pitr.object_store import RemoteObjectAck
from services.pitr.store_factory import (
    PitrStoreGroup,
    get_group_constructor_named,
    get_store_group,
)
from services.pitr.token_manager import (
    TokenHealth,
    TokenState,
    read_token_state,
    write_token_state,
)
from services.pitr.uploader import AckManifest, ack_manifest_from_raw


def _ack(value: bytes = b"ciphertext") -> RemoteObjectAck:
    return RemoteObjectAck(
        object_name="p/wal/00000001/000000010000000000000001.enc",
        pin_token="12345",  # noqa: S106 — test fixture identity
        size=len(value),
        checksum=ObjectChecksum(CRC32C, digest_bytes(CRC32C, value)),
        metadata={"ava-key-id": "v1"},
        created=True,
    )


# ── ACK shape ──


def test_ack_carries_pin_token_and_algo_value_checksum() -> None:
    ack = _ack()
    assert ack.pin_token == "12345"  # noqa: S105 — test fixture identity
    assert ack.checksum == ObjectChecksum(CRC32C, digest_bytes(CRC32C, b"ciphertext"))
    assert ack.created is True


def test_unknown_checksum_algo_fails_fast() -> None:
    with pytest.raises(ValueError, match="unsupported checksum algorithm"):
        ObjectChecksum("sha1", "x")


# ── checksum dispatch ──


def test_checksum_dispatch_covers_both_vocabularies() -> None:
    data = b"payload"
    assert {CRC32C, MD5} == KNOWN_CHECKSUM_ALGOS
    assert matches(ObjectChecksum(CRC32C, digest_bytes(CRC32C, data)), data)
    assert matches(ObjectChecksum(MD5, digest_bytes(MD5, data)), data)
    assert not matches(ObjectChecksum(MD5, digest_bytes(MD5, data)), data + b"x")
    assert not matches(ObjectChecksum(CRC32C, digest_bytes(MD5, data)), data)


def test_digest_of_unknown_algo_fails_fast() -> None:
    with pytest.raises(ValueError, match="unsupported checksum algorithm"):
        digest_bytes("sha256", b"x")


# ── legacy manifest compat (QA #1131 P1: pre-abstraction shapes) ──

_LEGACY_CANDIDATE_JSON = (
    '{"base_object":{"ciphertext_crc32c":"viqqbw==","ciphertext_size":4101269456,'
    '"encryption_format":"AVAPITRB1","generation":1788085003231815,'
    '"key_id":"ava-pitr-backup-key-prod",'
    '"object_name":"ava-pitr/base/activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40/'
    '358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac/base.tar.zst.enc",'
    '"source_sha256":"358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac",'
    '"source_size":6319665156},'
    '"chain_id":"activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40",'
    '"database_name":"ava_main","end_lsn":"A4/89EC6820",'
    '"migration_set_sha256":"63124a552737c95e0296cd29a5247cec07c1014d9eb474ea2d78116c73849f2e",'
    '"native_manifest_container_generation":1788085003231815,'
    '"native_manifest_container_object_name":'
    '"ava-pitr/base/activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40/'
    '358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac/base.tar.zst.enc",'
    '"native_manifest_member_path":"backup_manifest",'
    '"native_manifest_sha256":"5ee47ac3e20907e70894bf2761395256a78b94c116efe11f86ec26adff2153d2",'
    '"postgres_major":17,"protected":false,"schema_version":1,'
    '"start_lsn":"A4/7FC179B0","system_identifier":"7656686487711429617",'
    '"timeline":1,'
    '"wal_ranges":[{"end_lsn":"A4/89EC6820","start_lsn":"A4/7FC179B0","timeline":1}],'
    '"wal_segment_size":16777216}'
)


def test_legacy_candidate_manifest_normalizes_to_the_new_shape() -> None:
    """The exact pre-abstraction candidate JSON from the live tree must
    parse, and its renamed fields must land where the new code reads them
    (QA #1131 P1 — a deploy that rejects this file crash-loops the
    base-candidate daemon at boot)."""
    from services.pitr.base_manifest import CandidateManifest

    candidate = CandidateManifest.from_json(_LEGACY_CANDIDATE_JSON)
    assert candidate.base_object.pin_token == "1788085003231815"  # noqa: S105 — fixture
    assert candidate.base_object.ciphertext_checksum_algo == CRC32C
    assert candidate.base_object.ciphertext_checksum_value == "viqqbw=="
    assert candidate.native_manifest_container_pin_token == "1788085003231815"  # noqa: S105 — fixture
    assert candidate.chain_id == "activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40"
    # The new canonical serialization is stable across the rename.
    assert CandidateManifest.from_json(candidate.to_json()) == candidate


def test_legacy_protected_manifest_normalizes_restore_objects() -> None:
    from services.pitr.base_manifest import CandidateManifest
    from services.pitr.restore_manifest import (
        ProtectedManifest,
        _restore_object,
        required_archive_names,
    )

    legacy_restore_object = {
        "archive_name": "000000010000000000000001",
        "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
        "generation": 123,
        "size": 10,
        "crc32c": "crc-value",
        "metadata": [["ava-key-id", "v1"]],
    }
    normalized = _restore_object(legacy_restore_object)
    assert normalized.pin_token == "123"  # noqa: S105 — test fixture identity
    assert normalized.checksum_algo == CRC32C
    assert normalized.checksum_value == "crc-value"
    # A protected manifest embedding the legacy candidate parses end to end.
    candidate = CandidateManifest.from_json(_LEGACY_CANDIDATE_JSON)
    proof = RestoreProofFixture()
    names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    protected_raw: dict[str, Any] = {
        "schema_version": 1,
        "protected": True,
        "chain_id": candidate.chain_id,
        "candidate_sha256": candidate_sha256_of(candidate),
        "candidate": json.loads(_LEGACY_CANDIDATE_JSON),
        "base": {
            "archive_name": "base.tar.zst.enc",
            "object_name": candidate.base_object.object_name,
            "generation": int(candidate.base_object.pin_token),
            "size": candidate.base_object.ciphertext_size,
            "crc32c": candidate.base_object.ciphertext_checksum_value,
            "metadata": [],
        },
        "wal": [
            {
                "archive_name": name,
                "object_name": f"ava-pitr/wal/{name[:8]}/{name}.enc",
                "generation": index + 1,
                "size": 10,
                "crc32c": "crc-value",
                "metadata": [["ava-key-id", "v1"]],
            }
            for index, name in enumerate(names)
        ],
        "target_lsn": candidate.end_lsn,
        "wal_segment_size": candidate.wal_segment_size,
        "proof": proof.__dict__,
    }
    protected = ProtectedManifest.from_json(json.dumps(protected_raw))
    assert protected.base.pin_token == str(candidate.base_object.pin_token)
    assert protected.candidate == candidate


def test_resume_protected_publish_accepts_the_legacy_bytes_digest(tmp_path: Path) -> None:
    """QA #1131 delta2: the durable resume path accepts a protected manifest
    written before the abstraction — the embedded candidate keeps its legacy
    serialization and the digest covers those raw bytes (ef05a1e7...).
    from_json canonicalizes the field, so the resume check is a plain
    canonical equality, never a re-hash of a digest string."""
    import hashlib

    from services.pitr.base_manifest import CandidateManifest
    from services.pitr.restore_manifest import required_archive_names
    from services.pitr.restore_proof import _resume_protected_publish

    legacy_digest = hashlib.sha256(_LEGACY_CANDIDATE_JSON.encode()).hexdigest()
    # The full live-tree legacy-bytes digest, pinned verbatim (QA #1131 nit).
    assert legacy_digest == "ef05a1e70c743e454be4e745fe04fd5e2becf12a55dfc84e8aafd7e576ef3f1d"
    candidate = CandidateManifest.from_json(_LEGACY_CANDIDATE_JSON)
    names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    protected_raw: dict[str, Any] = {
        "schema_version": 1,
        "protected": True,
        "chain_id": candidate.chain_id,
        "candidate_sha256": legacy_digest,
        "candidate": json.loads(_LEGACY_CANDIDATE_JSON),
        "base": {
            "archive_name": "base.tar.zst.enc",
            "object_name": candidate.base_object.object_name,
            "generation": int(candidate.base_object.pin_token),
            "size": candidate.base_object.ciphertext_size,
            "crc32c": candidate.base_object.ciphertext_checksum_value,
            "metadata": [],
        },
        "wal": [
            {
                "archive_name": name,
                "object_name": f"ava-pitr/wal/{name[:8]}/{name}.enc",
                "generation": index + 1,
                "size": 10,
                "crc32c": "crc-value",
                "metadata": [["ava-key-id", "v1"]],
            }
            for index, name in enumerate(names)
        ],
        "target_lsn": candidate.end_lsn,
        "wal_segment_size": candidate.wal_segment_size,
        "proof": RestoreProofFixture().__dict__,
    }
    root = tmp_path / "root"
    (root / "protected-manifests").mkdir(parents=True)
    (root / "protected-manifests" / f"{candidate.chain_id}.json").write_text(
        json.dumps(protected_raw)
    )

    class Publisher:
        def put_manifest_if_absent(
            self, *, payload: bytes, object_name: str, metadata: dict[str, str]
        ) -> RemoteObjectAck:
            raise AssertionError("resume of a local manifest must not republish")

    resumed = _resume_protected_publish(
        root=root, candidate=candidate, prefix="ava-pitr", publisher=Publisher()
    )
    assert resumed is not None
    assert resumed.candidate == candidate
    assert resumed.candidate_sha256 == candidate_sha256_of(candidate)


def test_resume_protected_publish_rejects_same_chain_different_candidate(
    tmp_path: Path,
) -> None:
    """QA #1131 delta2 counterexample: a same-chain_id manifest whose
    embedded candidate differs in content must be rejected — the previous
    hash-of-hash check let every new-shape manifest through regardless of
    what it embedded."""
    import dataclasses

    from services.pitr.base_manifest import CandidateManifest
    from services.pitr.restore_manifest import required_archive_names
    from services.pitr.restore_proof import RestoreProofError, _resume_protected_publish

    candidate = CandidateManifest.from_json(_LEGACY_CANDIDATE_JSON)
    tampered = dataclasses.replace(candidate, system_identifier="7656686487711429999")
    names = required_archive_names(tampered.wal_ranges, tampered.wal_segment_size)
    protected_raw: dict[str, Any] = {
        "schema_version": 1,
        "protected": True,
        "chain_id": tampered.chain_id,
        "candidate_sha256": candidate_sha256_of(tampered),
        "candidate": json.loads(tampered.to_json()),
        "base": {
            "archive_name": "base.tar.zst.enc",
            "object_name": tampered.base_object.object_name,
            "pin_token": tampered.base_object.pin_token,
            "size": tampered.base_object.ciphertext_size,
            "checksum_algo": tampered.base_object.ciphertext_checksum_algo,
            "checksum_value": tampered.base_object.ciphertext_checksum_value,
            "metadata": [],
        },
        "wal": [
            {
                "archive_name": name,
                "object_name": f"ava-pitr/wal/{name[:8]}/{name}.enc",
                "generation": index + 1,
                "size": 10,
                "crc32c": "crc-value",
                "metadata": [["ava-key-id", "v1"]],
            }
            for index, name in enumerate(names)
        ],
        "target_lsn": tampered.end_lsn,
        "wal_segment_size": tampered.wal_segment_size,
        "proof": RestoreProofFixture().__dict__,
    }
    root = tmp_path / "root"
    (root / "protected-manifests").mkdir(parents=True)
    (root / "protected-manifests" / f"{tampered.chain_id}.json").write_text(
        json.dumps(protected_raw)
    )

    class Publisher:
        def put_manifest_if_absent(
            self, *, payload: bytes, object_name: str, metadata: dict[str, str]
        ) -> RemoteObjectAck:
            raise AssertionError("rejected resume must not republish")

    with pytest.raises(RestoreProofError, match="does not match its candidate"):
        _resume_protected_publish(
            root=root, candidate=candidate, prefix="ava-pitr", publisher=Publisher()
        )


def test_legacy_retention_plan_normalizes_retention_objects() -> None:
    from services.pitr.retention_manifest import RetentionPlan

    legacy_plan = {
        "schema_version": 1,
        "retained_chain_count": 2,
        "evidence_sha256": "e" * 64,
        "protected_chain_ids": ["a", "b"],
        "unprotected_chain_ids": [],
        "oldest_retained_chain_id": "a",
        "ack_high_water": None,
        "blocked_reasons": [],
        "retained": [
            {
                "object": {
                    "object_name": "ava-pitr/wal/00000001/x.enc",
                    "generation": 7,
                    "size": 10,
                    "archive_name": "000000010000000000000001",
                    "kind": "wal",
                    "crc32c": "crc-value",
                    "metadata": [["ava-key-id", "v1"]],
                },
                "reason": "continuous WAL recovery window",
            }
        ],
        "eligible": [],
        "retained_bytes": 10,
        "eligible_bytes": 0,
    }
    plan = RetentionPlan.from_json(json.dumps(legacy_plan))
    assert plan.retained[0].object.pin_token == "7"  # noqa: S105 — test fixture identity
    assert plan.retained[0].object.checksum_algo == CRC32C
    assert plan.retained[0].object.checksum_value == "crc-value"


class RestoreProofFixture:
    def __init__(self) -> None:
        self.__dict__ = {
            "run_id": "run",
            "started_at": "2026-08-30T03:00:00+00:00",
            "completed_at": "2026-08-30T03:01:00+00:00",
            "target_lsn": "A4/89EC6820",
            "achieved_lsn": "A4/89EC6820",
            "live_postgres_pid": 1,
            "live_probe_sha256": "live",
            "candidate_verify_evidence_sha256": "candidate-verify",
            "replay_seconds": 2,
            "smoke_seconds": 3,
            "restored_verify_seconds": 4,
            "downloaded_bytes": 100,
            "restored_fingerprint_sha256": "restored",
        }


def candidate_sha256_of(candidate: object) -> str:
    import hashlib

    from services.pitr.base_manifest import CandidateManifest

    return hashlib.sha256(
        CandidateManifest.to_json(candidate).encode()  # type: ignore[arg-type]
    ).hexdigest()


# ── legacy ACK compat (738 real on-disk ACKs predate the abstraction) ──


def _legacy_raw() -> dict[str, Any]:
    return {
        "archive_name": "000000010000000000000001",
        "source_sha256": "a" * 64,
        "source_size": 16,
        "object_name": "p/wal/00000001/000000010000000000000001.enc",
        "generation": 123456,
        "ciphertext_size": 100,
        "ciphertext_crc32c": "crc32c-value",
        "encryption_format": "AVAPITR1",
        "key_id": "v1",
        "acknowledged_at": "2026-08-30T10:00:00+00:00",
    }


def test_legacy_ack_normalizes_to_pin_token_and_crc32c_checksum() -> None:
    ack = ack_manifest_from_raw(_legacy_raw())
    assert ack.pin_token == "123456"  # noqa: S105 — test fixture identity
    assert ack.ciphertext_checksum_algo == CRC32C
    assert ack.ciphertext_checksum_value == "crc32c-value"


def test_fresh_ack_shape_round_trips_untouched() -> None:
    raw = _legacy_raw()
    raw.pop("generation")
    raw["pin_token"] = "fs123:md5"  # noqa: S105 — test fixture identity
    raw["ciphertext_checksum_algo"] = MD5
    raw["ciphertext_checksum_value"] = "0" * 32
    raw["ciphertext_crc32c"] = "crc32c-value"
    ack = ack_manifest_from_raw(raw)
    assert ack.pin_token == "fs123:md5"  # noqa: S105 — test fixture identity
    assert ack.ciphertext_checksum_algo == MD5
    assert ack.ciphertext_checksum_value == "0" * 32
    assert ack.ciphertext_crc32c == "crc32c-value"


def test_fresh_ack_without_local_digest_fails_closed_for_non_crc32c() -> None:
    raw = _legacy_raw()
    raw.pop("generation")
    raw.pop("ciphertext_crc32c")
    raw["pin_token"] = "fs123:md5"  # noqa: S105 — test fixture identity
    raw["ciphertext_checksum_algo"] = MD5
    raw["ciphertext_checksum_value"] = "0" * 32
    with pytest.raises(TypeError, match="local ciphertext digest"):
        ack_manifest_from_raw(raw)


def test_ack_without_any_pin_identity_fails_closed() -> None:
    raw = _legacy_raw()
    raw.pop("generation")
    with pytest.raises(TypeError, match="pin token"):
        ack_manifest_from_raw(raw)


def test_ack_manifest_rejects_mixed_unknown_fields() -> None:
    raw = _legacy_raw()
    raw["surprise"] = True
    with pytest.raises(TypeError):
        AckManifest(**raw)  # the strict constructor stays strict


# ── factory ──


def _service_account(email: str = "uploader@example.com") -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    payload = {
        "type": "service_account",
        "client_email": email,
        "project_id": "project",
        "private_key_id": "key",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return json.dumps(payload)


def test_factory_constructs_every_role_for_the_gcs_backend(tmp_path: Path) -> None:
    credentials = tmp_path / "gcs.json"
    credentials.write_text(_service_account())
    group = get_group_constructor_named("gcs")(
        project="p",
        bucket="b",
        prefix="ava-pitr",
        uploader_credentials=credentials,
        viewer_credentials=credentials,
    )
    assert group.object_store() is not None
    assert group.restartable_streaming_object_store() is not None
    assert group.viewer_object_store() is not None
    assert group.generation_pinned_object_reader() is not None
    assert group.retention_inventory_reader() is not None
    assert group.protected_manifest_publisher() is not None


def test_factory_constructs_every_role_for_the_baidu_backend(tmp_path: Path) -> None:
    credentials = tmp_path / "baidu.json"
    credentials.write_text(
        json.dumps({"app_key": "app", "secret_key": "secret", "refresh_token": "refresh"})
    )
    token_file = tmp_path / "token.json"
    group = get_group_constructor_named("baidu")(
        app_root="/apps/ava/ava-pitr",
        prefix="ava-pitr",
        credentials_file=credentials,
        token_file=token_file,
    )
    assert group.object_store() is not None
    assert group.restartable_streaming_object_store() is not None
    assert group.viewer_object_store() is not None
    assert group.generation_pinned_object_reader() is not None
    assert group.retention_inventory_reader() is not None
    assert group.protected_manifest_publisher() is not None


def test_factory_rejects_unknown_backend_without_falling_back() -> None:
    with pytest.raises(ValueError, match=r"unknown PITR store backend 's3' \(known: baidu, gcs\)"):
        get_group_constructor_named("s3")
    with pytest.raises(ValueError, match="unknown PITR store backend"):
        get_group_constructor_named("")


def test_factory_reads_the_configured_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "gcs")
    assert isinstance(get_store_group(), PitrStoreGroup)
    credentials = tmp_path / "baidu.json"
    credentials.write_text(
        json.dumps({"app_key": "app", "secret_key": "secret", "refresh_token": "refresh"})
    )
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "baidu")
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_credentials_file", credentials)
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_token_file", tmp_path / "token.json")
    assert isinstance(get_store_group(), PitrStoreGroup)
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "nope")
    with pytest.raises(ValueError, match="unknown PITR store backend"):
        get_store_group()


# ── token manager skeleton ──


def test_token_state_validates_expiry_and_remaining() -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    state = TokenState("access", "refresh", now + timedelta(days=30))
    assert state.remaining_seconds(now) == 30 * 86400
    with pytest.raises(ValueError, match="timezone-aware"):
        TokenState("access", "refresh", datetime(2026, 8, 30, 10, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="must not be empty"):
        TokenState("", "refresh", now + timedelta(days=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        state.remaining_seconds(datetime(2026, 8, 30, 10, 0))  # noqa: DTZ001


def test_token_state_persists_atomically_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    now = datetime.now(UTC) + timedelta(days=30)
    state = TokenState("access-token", "refresh-token", now)
    write_token_state(path, state)
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = read_token_state(path)
    assert loaded == state
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(f".{path.name}.")]


def test_token_state_read_fails_closed_on_garbage(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text('{"access_token": 7, "expires_at": "not-a-time"}')
    with pytest.raises((TypeError, ValueError)):
        read_token_state(path)
    path.write_text("not json")
    with pytest.raises(ValueError):
        read_token_state(path)


def test_token_health_defaults_to_unprovisioned() -> None:
    health = TokenHealth(
        remaining_seconds=None, expires_at=None, last_refresh_at=None, refresh_error=None
    )
    assert health.remaining_seconds is None


def test_ack_serializes_with_pin_token_not_generation() -> None:
    ack = _ack()
    assert hasattr(ack, "pin_token") and not hasattr(ack, "generation")
    assert ack.checksum.algo == CRC32C
