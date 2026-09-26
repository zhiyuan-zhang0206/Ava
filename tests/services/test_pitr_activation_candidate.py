"""Regression coverage for PITR activation's candidate-validation boundary."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cli.commands import _pitr_activation_config as activation_config
from services.pitr.activation_state import ActivationRecord
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

    record = replace(
        ActivationRecord.start(operation_id="test", origin="test"),
        pre_activation_env_b64=base64.b64encode(before).decode(),
        pre_activation_env_digest=hashlib.sha256(before).hexdigest(),
    )
    with pytest.raises(RuntimeError, match="candidate is invalid"):
        activation_config._apply_env(tmp_path, record)

    assert env_path.read_bytes() == before
