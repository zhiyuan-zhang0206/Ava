"""HTTP regression coverage for candidate validation before cluster config writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import runtime_config


def _write_private_file(path: Path, content: str | bytes) -> Path:
    path.write_text(content) if isinstance(content, str) else path.write_bytes(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def gcs_restore_proof_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A valid GCS restore-proof state with all non-viewer OSS inputs present."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    backup_key = _write_private_file(tmp_path / "backup.key", b"k" * 32)
    gcs_uploader = _write_private_file(
        tmp_path / "gcs-uploader.json",
        json.dumps(
            {
                "type": "service_account",
                "client_email": "uploader@example.com",
                "project_id": "test-project",
                "private_key_id": "test-key",
            }
        ),
    )
    gcs_viewer = _write_private_file(
        tmp_path / "gcs-viewer.json",
        json.dumps(
            {
                "type": "service_account",
                "client_email": "viewer@example.com",
                "project_id": "test-project",
                "private_key_id": "test-key",
            }
        ),
    )
    oss_uploader = _write_private_file(
        tmp_path / "oss-uploader.json",
        json.dumps({"access_key_id": "upload", "access_key_secret": "test-upload-secret"}),
    )
    runtime_config.write_fields(
        {
            "pitr_enabled": True,
            "pitr_base_backup_enabled": True,
            "pitr_restore_proof_enabled": True,
            "pitr_store_backend": "gcs",
            "pitr_gcs_project": "test-project",
            "pitr_gcs_bucket": "test-bucket",
            "pitr_backup_key_file": backup_key,
            "pitr_backup_key_id": "test-key",
            "pitr_replication_db_url": "postgresql://replicator@127.0.0.1:5432/postgres",
            "pitr_gcs_credentials_file": gcs_uploader,
            "pitr_restore_gcs_credentials_file": gcs_viewer,
            "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
            "pitr_oss_bucket": "test-bucket",
            "pitr_oss_credentials_file": oss_uploader,
        },
        set(),
    )
    return tmp_path


def test_put_rejects_invalid_oss_candidate_without_writing(
    gcs_restore_proof_home: Path,
) -> None:
    """A backend switch cannot persist an OSS restore-proof state without a viewer."""
    env_path = gcs_restore_proof_home / ".env"
    before = env_path.read_bytes()

    with TestClient(app) as client:
        response = client.put("/api/config", json={"pitr_store_backend": "oss"})

    assert response.status_code == 400, response.text
    assert "candidate config rejected" in response.json()["detail"]
    assert "AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE" in response.json()["detail"]
    assert env_path.read_bytes() == before
