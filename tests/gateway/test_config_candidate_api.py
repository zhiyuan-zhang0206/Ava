"""HTTP regression coverage for candidate validation before cluster config writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import config as config_router
from shared import runtime_config
from shared.config.candidate import EnvPatchValidation


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


def test_put_full_oss_atomic_patch_succeeds(gcs_restore_proof_home: Path) -> None:
    """The complete OSS transition lands in one atomic PUT — backend, endpoint,
    bucket, uploader credentials, and a distinct viewer — with restore proof
    validating. This is the exact patch shape the prod OSS switch executes."""
    oss_viewer = _write_private_file(
        gcs_restore_proof_home / "oss-viewer.json",
        json.dumps({"access_key_id": "view", "access_key_secret": "test-view-secret"}),
    )
    with TestClient(app) as client:
        response = client.put(
            "/api/config",
            json={
                "pitr_store_backend": "oss",
                "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
                "pitr_oss_bucket": "test-bucket",
                "pitr_oss_credentials_file": str(gcs_restore_proof_home / "oss-uploader.json"),
                "pitr_oss_viewer_credentials_file": str(oss_viewer),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert "gateway" in body["restart_required"]
    aliases = runtime_config.read_env_aliases()
    assert aliases["AVA_PITR_STORE_BACKEND"] == "oss"
    assert aliases["AVA_PITR_OSS_ENDPOINT"] == "https://oss-cn-shanghai.aliyuncs.com"
    assert aliases["AVA_PITR_OSS_BUCKET"] == "test-bucket"
    assert aliases["AVA_PITR_OSS_CREDENTIALS_FILE"] == str(
        gcs_restore_proof_home / "oss-uploader.json"
    )
    assert aliases["AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE"] == str(oss_viewer)


def test_host_only_put_skips_cluster_candidate_validation(
    gcs_restore_proof_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-only edit cannot fail because an empty cluster patch went stale."""

    def unexpected_cluster_validation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-only PUT must not validate a cluster candidate")

    monkeypatch.setattr(
        config_router, "validate_env_patch_for_write", unexpected_cluster_validation
    )

    with TestClient(app) as client:
        response = client.put("/api/config", json={"ops_concurrency": 4})

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert runtime_config.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "4"


def test_put_returns_conflict_when_cluster_candidate_goes_stale(
    gcs_restore_proof_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write between validation and persistence is reported as a retryable 409."""
    original_validate = config_router.validate_env_patch_for_write
    validation_calls = 0

    def validate_then_change_file(
        updates: dict[str, object], removals: set[str]
    ) -> EnvPatchValidation:
        nonlocal validation_calls
        candidate = original_validate(updates, removals)
        validation_calls += 1
        if validation_calls == 2:
            runtime_config.write_fields({"llm_model": "concurrent-update"}, set())
        return candidate

    monkeypatch.setattr(config_router, "validate_env_patch_for_write", validate_then_change_file)

    with TestClient(app) as client:
        response = client.put("/api/config", json={"llm_model": "requested-update"})

    assert validation_calls == 2
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "config changed concurrently; retry the request"
