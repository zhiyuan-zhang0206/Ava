"""Regression coverage for PITR activation's candidate-validation boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.pitr import activation_runtime
from shared import runtime_config


def _write_private_file(path: Path, content: str | bytes) -> Path:
    path.write_text(content) if isinstance(content, str) else path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_enable_pitr_services_refuses_incomplete_oss_restore_proof_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation refuses a pre-existing OSS state without a viewer credential."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    backup_key = _write_private_file(tmp_path / "backup.key", b"k" * 32)
    oss_uploader = _write_private_file(
        tmp_path / "oss-uploader.json",
        json.dumps({"access_key_id": "upload", "access_key_secret": "test-upload-secret"}),
    )
    runtime_config.write_fields(
        {
            "pitr_store_backend": "oss",
            "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
            "pitr_oss_bucket": "test-bucket",
            "pitr_oss_credentials_file": oss_uploader,
            "pitr_backup_key_file": backup_key,
            "pitr_backup_key_id": "test-key",
            "pitr_replication_db_url": "postgresql://replicator@127.0.0.1:5432/postgres",
        },
        set(),
    )
    env_path = tmp_path / ".env"
    before = env_path.read_bytes()

    with pytest.raises(RuntimeError, match="PITR activation refused"):
        activation_runtime._enable_pitr_services(hashlib.sha256(before).hexdigest())

    assert env_path.read_bytes() == before
