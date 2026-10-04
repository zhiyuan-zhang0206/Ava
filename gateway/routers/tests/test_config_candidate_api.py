"""HTTP regression coverage for candidate validation before cluster config writes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from base.config.admin.candidate import EnvPatchValidation
from base.host.env import runtime_config
from gateway.app import app
from gateway.routers import config as config_router


@pytest.fixture
def configured_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A gateway home whose `.env` carries a valid sandbox timeout pair."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    runtime_config.write_fields(
        {"exec_timeout_seconds": 300, "exec_node_timeout_seconds": 1200}, set()
    )
    return tmp_path


def test_put_rejects_invalid_candidate_without_writing(configured_home: Path) -> None:
    """A patch that breaks a cross-field invariant never reaches the `.env`."""
    env_path = configured_home / ".env"
    before = env_path.read_bytes()

    with TestClient(app) as client:
        response = client.put("/api/config", json={"exec_node_timeout_seconds": 200})

    assert response.status_code == 400, response.text
    assert "candidate config rejected" in response.json()["detail"]
    assert "exec_node_timeout_seconds" in response.json()["detail"]
    assert env_path.read_bytes() == before


def test_put_sets_the_offsite_backup_destination_beside_retired_keys(
    configured_home: Path,
) -> None:
    """The daily dump's destination lands in one atomic PUT, the exact patch the
    rollout runs through `ava config set`. Keys a retired feature left in the
    `.env` are inert: they neither block the write nor change how it validates."""
    env_path = configured_home / ".env"
    env_path.write_text(
        env_path.read_text()
        + "AVA_PITR_ENABLED=true\n"
        + "AVA_PITR_STORE_BACKEND=oss\n"
        + "AVA_PITR_GCS_BUCKET=retired\n"
        + "AVA_PITR_SPOOL_HARD_BYTES=2362232013\n"
    )
    credentials = configured_home / "oss-credentials.json"
    with TestClient(app) as client:
        response = client.put(
            "/api/config",
            json={
                "backup_offsite_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
                "backup_offsite_bucket": "backups",
                "backup_offsite_credentials_file": str(credentials),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert "gateway" in body["restart_required"]
    aliases = runtime_config.read_env_aliases()
    assert aliases["AVA_BACKUP_OFFSITE_ENDPOINT"] == "https://oss-cn-shanghai.aliyuncs.com"
    assert aliases["AVA_BACKUP_OFFSITE_BUCKET"] == "backups"
    assert aliases["AVA_BACKUP_OFFSITE_CREDENTIALS_FILE"] == str(credentials)
    assert aliases["AVA_PITR_STORE_BACKEND"] == "oss"  # the retired keys stay as they were


def test_put_unsets_the_offsite_backup_destination(configured_home: Path) -> None:
    """The destination is removed through the same official path."""
    runtime_config.write_fields(
        {"backup_offsite_endpoint": "https://oss-cn-shanghai.aliyuncs.com"}, set()
    )

    with TestClient(app) as client:
        response = client.put("/api/config", json={"backup_offsite_endpoint": None})

    assert response.status_code == 200, response.text
    assert "AVA_BACKUP_OFFSITE_ENDPOINT" not in runtime_config.read_env_aliases()


def test_host_only_put_skips_cluster_candidate_validation(
    configured_home: Path, monkeypatch: pytest.MonkeyPatch
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
    configured_home: Path, monkeypatch: pytest.MonkeyPatch
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
