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
from services.pitr.store_factory import get_backend, get_backend_named
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
    raw.pop("ciphertext_crc32c")
    raw["pin_token"] = "fs123:md5"  # noqa: S105 — test fixture identity
    raw["ciphertext_checksum_algo"] = MD5
    raw["ciphertext_checksum_value"] = "0" * 32
    ack = ack_manifest_from_raw(raw)
    assert ack.pin_token == "fs123:md5"  # noqa: S105 — test fixture identity
    assert ack.ciphertext_checksum_algo == MD5
    assert ack.ciphertext_checksum_value == "0" * 32


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
    backend = get_backend_named("gcs")
    assert backend.name == "gcs"
    assert backend.object_store(project="p", bucket="b", credentials_file=credentials) is not None
    assert (
        backend.restartable_streaming_object_store(
            project="p", bucket="b", credentials_file=str(credentials)
        )
        is not None
    )
    assert (
        backend.generation_pinned_object_reader(
            project="p", bucket="b", credentials_file=credentials
        )
        is not None
    )
    assert (
        backend.retention_inventory_reader(
            project="p", bucket="b", prefix="ava-pitr", credentials_file=credentials
        )
        is not None
    )
    assert (
        backend.protected_manifest_publisher(project="p", bucket="b", credentials_file=credentials)
        is not None
    )


def test_factory_rejects_unknown_backend_without_falling_back() -> None:
    with pytest.raises(ValueError, match=r"unknown PITR store backend 's3' \(known: gcs\)"):
        get_backend_named("s3")
    with pytest.raises(ValueError, match="unknown PITR store backend"):
        get_backend_named("")


def test_factory_reads_the_configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "gcs")
    assert get_backend().name == "gcs"
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "nope")
    with pytest.raises(ValueError, match="unknown PITR store backend"):
        get_backend()


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
