"""Unit tests for the WAL remote-proof evidence chain (QA #1147 C1).

The proof chain dispatches on the credential-evidence backend: the GCS
vocabulary (bucket name, generation number, CRC32C base64) must keep
validating exactly as before, and the Baidu vocabulary (app-root store
target, ``fs_id:md5`` pin token, content-MD5 checksum) must clear the
same gates instead of wedging the activation.
"""

from __future__ import annotations

import pytest

from services.pitr.activation_evidence import validate_wal_remote_evidence
from services.pitr.activation_runtime import wal_evidence_common, wal_metadata
from services.pitr.uploader import AckManifest
from shared.config import settings
from tests._pitr_fixtures import baidu_credential_evidence

_SEGMENT = "00000001000000A20000008B"
_MD5_HEX = "b" * 32
_PIN_TOKEN = "123456789:" + _MD5_HEX
_CRC32C = "AAAAAA=="


def _gcs_credentials() -> dict[str, str]:
    return {
        "backend": "gcs",
        "uploader_identity": "u@example.test",
        "viewer_identity": "v@example.test",
        "store_target": "bucket",
        "object_prefix": "pitr",
        "backup_key_id": "key",
        "backup_key_sha256": "0" * 64,
    }


def _baidu_ack() -> dict[str, str]:
    return {
        "timeline": "1",
        "segment": _SEGMENT,
        "bucket_name": "/apps/ava/ava-pitr",
        "object_prefix": "pitr",
        "object_name": f"pitr/wal/{_SEGMENT[:8]}/{_SEGMENT}.enc",
        "generation": _PIN_TOKEN,
        "ciphertext_size": "10",
        "ciphertext_crc32c": _MD5_HEX,
        "source_sha256": "1" * 64,
        "source_size": "1",
        "key_id": "key",
        "encryption_format": "AVAPITR1",
        "acknowledged_at": "2026-08-30T03:30:00+00:00",
    }


def _baidu_viewer() -> dict[str, str]:
    return {
        **{k: v for k, v in _baidu_ack().items() if k != "acknowledged_at"},
        "viewer_id": "app-key",
        "observed_at": "2026-08-30T03:31:00+00:00",
    }


def _exact() -> dict[str, str]:
    return {
        "timeline": "1",
        "segment": _SEGMENT,
        "switch_lsn": "0/1",
        "failed_count": "0",
        "archived_count": "0",
        "switch_intent_at": "2026-08-30T03:00:00+00:00",
    }


def _validate(*, ack: dict[str, str], viewer: dict[str, str]) -> None:
    validate_wal_remote_evidence(
        ack=ack,
        viewer=viewer,
        exact=_exact(),
        verification_deadline="2026-08-30T04:00:00+00:00",
        credential_evidence=baidu_credential_evidence(),
    )


def _pair(**overrides: str) -> tuple[dict[str, str], dict[str, str]]:
    """A Baidu ACK/viewer pair with the same tampering applied to both
    sides, so the mutation reaches the gate under test instead of the
    ack/viewer immutable cross-check."""
    return {**_baidu_ack(), **overrides}, {**_baidu_viewer(), **overrides}


def test_baidu_wal_proof_validates_through_all_three_gates() -> None:
    """A Baidu-shaped ACK/viewer pair must clear the store-target, pin-token,
    and checksum gates together."""
    _validate(ack=_baidu_ack(), viewer=_baidu_viewer())


def test_baidu_wal_proof_rejects_store_target_drift() -> None:
    """Gate 1: the proof's store target must equal the frozen app root."""
    ack, viewer = _pair(bucket_name="")
    with pytest.raises(ValueError, match="differs from WAL intent"):
        _validate(ack=ack, viewer=viewer)


def test_baidu_wal_proof_rejects_gcs_shaped_pin_token() -> None:
    """Gate 2: a bare generation number cannot serve as a Baidu pin token."""
    ack, viewer = _pair(generation="123")
    with pytest.raises(ValueError, match="invalid Baidu pin token"):
        _validate(ack=ack, viewer=viewer)


def test_baidu_wal_proof_rejects_pin_token_md5_that_differs_from_the_checksum() -> None:
    """QA #1147 nit 2: the pin's embedded content md5 must equal the
    checksum field's md5 — a drift between the two is evidence corruption."""
    ack, viewer = _pair(generation="123456789:" + "c" * 32)
    with pytest.raises(ValueError, match="differs from the ciphertext checksum"):
        _validate(ack=ack, viewer=viewer)


def test_baidu_wal_proof_rejects_crc32c_shaped_checksum() -> None:
    """Gate 3: the Baidu checksum must be hex MD5, not CRC32C base64."""
    ack, viewer = _pair(ciphertext_crc32c=_CRC32C)
    with pytest.raises(ValueError, match="invalid ciphertext MD5"):
        _validate(ack=ack, viewer=viewer)


def test_baidu_wal_proof_rejects_non_hex_md5_checksum() -> None:
    """Gate 3: a 32-character non-hex value is not a content MD5 either."""
    ack, viewer = _pair(ciphertext_crc32c="z" * 32)
    with pytest.raises(ValueError, match="invalid ciphertext MD5"):
        _validate(ack=ack, viewer=viewer)


def test_gcs_wal_proof_keeps_validating_legacy_shapes() -> None:
    """The GCS vocabulary must keep clearing the same gates unchanged."""
    ack = {
        **_baidu_ack(),
        "bucket_name": "bucket",
        "generation": "123",
        "ciphertext_crc32c": _CRC32C,
    }
    viewer = {**ack, "viewer_id": "v@example.test", "observed_at": "2026-08-30T03:31:00+00:00"}
    validate_wal_remote_evidence(
        ack=ack,
        viewer=viewer,
        exact=_exact(),
        verification_deadline="2026-08-30T04:00:00+00:00",
        credential_evidence=_gcs_credentials(),
    )


def test_wal_proof_rejects_an_unknown_credential_backend() -> None:
    """The fail-fast gate: an unrecognized backend must refuse the proof
    before any vocabulary dispatch — a typo must never wedge a later gate
    or silently fall back to the previous backend."""
    ack, viewer = _pair()
    with pytest.raises(ValueError, match="unknown backend"):
        validate_wal_remote_evidence(
            ack=ack,
            viewer=viewer,
            exact=_exact(),
            verification_deadline="2026-08-30T04:00:00+00:00",
            credential_evidence={**baidu_credential_evidence(), "backend": "s3"},
        )


def _ack_manifest() -> AckManifest:
    return AckManifest(
        archive_name=_SEGMENT,
        source_sha256="1" * 64,
        source_size=1,
        object_name=f"pitr/wal/{_SEGMENT[:8]}/{_SEGMENT}.enc",
        pin_token=_PIN_TOKEN,
        ciphertext_size=10,
        ciphertext_crc32c=_CRC32C,
        ciphertext_checksum_algo="md5",
        ciphertext_checksum_value=_MD5_HEX,
        encryption_format="AVAPITR1",
        key_id="key",
        acknowledged_at="2026-08-30T03:30:00+00:00",
    )


def test_wal_evidence_common_uses_the_baidu_app_root_as_store_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "baidu")
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_app_root", "/apps/ava/ava-pitr")
    common = wal_evidence_common(
        ack=_ack_manifest(), exact=_exact(), config=settings.physical_backup
    )
    assert common["bucket_name"] == "/apps/ava/ava-pitr"
    assert common["generation"] == _PIN_TOKEN
    assert common["ciphertext_crc32c"] == _MD5_HEX


def test_wal_evidence_common_keeps_the_gcs_bucket_as_store_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "gcs")
    monkeypatch.setattr(settings.physical_backup, "pitr_gcs_bucket", "bucket")
    common = wal_evidence_common(
        ack=_ack_manifest(), exact=_exact(), config=settings.physical_backup
    )
    assert common["bucket_name"] == "bucket"


def test_wal_metadata_uses_the_local_crc32c_not_the_backend_checksum() -> None:
    """The metadata the viewer compares must carry the local plan digest —
    the uploader wrote it under this key, while the backend-verified
    checksum is a content MD5 on Baidu."""
    assert wal_metadata(_ack_manifest())["ava-ciphertext-crc32c"] == _CRC32C


def test_require_store_config_refuses_each_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.pitr.activation_credentials import require_store_config

    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_gcs_prefix", "pitr")
    monkeypatch.setattr(config, "pitr_store_backend", "baidu")
    monkeypatch.setattr(config, "pitr_baidu_app_root", "")
    with pytest.raises(RuntimeError, match="app root"):
        require_store_config(config)
    monkeypatch.setattr(config, "pitr_baidu_app_root", "/apps/ava/ava-pitr")
    require_store_config(config)
    monkeypatch.setattr(config, "pitr_store_backend", "gcs")
    monkeypatch.setattr(config, "pitr_gcs_bucket", "")
    with pytest.raises(RuntimeError, match="bucket"):
        require_store_config(config)
    monkeypatch.setattr(config, "pitr_gcs_bucket", "bucket")
    require_store_config(config)
    monkeypatch.setattr(config, "pitr_gcs_prefix", "")
    with pytest.raises(RuntimeError, match="prefix"):
        require_store_config(config)
