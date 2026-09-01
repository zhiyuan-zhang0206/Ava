"""Regression coverage for full-candidate validation before config persistence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from shared import runtime_config
from shared.config.candidate import EnvPatchValidation

type EnvPatchValidator = Callable[[dict[str, object], set[str]], list[str]]


def _write_private_file(path: Path, content: str | bytes) -> Path:
    path.write_text(content) if isinstance(content, str) else path.write_bytes(content)
    path.chmod(0o600)
    return path


def _service_account(email: str) -> str:
    return json.dumps(
        {
            "type": "service_account",
            "client_email": email,
            "project_id": "test-project",
            "private_key_id": "test-key",
        }
    )


def _oss_credentials(access_key_id: str) -> str:
    return json.dumps(
        {
            "access_key_id": access_key_id,
            "access_key_secret": f"secret-for-{access_key_id}",
        }
    )


@dataclass(frozen=True)
class _PitrFixture:
    oss_uploader: Path
    oss_viewer: Path


@pytest.fixture
def configured_gcs_pitr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _PitrFixture:
    """A valid restore-proof GCS candidate plus unused, real OSS credentials."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    backup_key = _write_private_file(tmp_path / "backup.key", b"k" * 32)
    gcs_uploader = _write_private_file(
        tmp_path / "gcs-uploader.json", _service_account("uploader@example.com")
    )
    gcs_viewer = _write_private_file(
        tmp_path / "gcs-viewer.json", _service_account("viewer@example.com")
    )
    oss_uploader = _write_private_file(tmp_path / "oss-uploader.json", _oss_credentials("upload"))
    oss_viewer = _write_private_file(tmp_path / "oss-viewer.json", _oss_credentials("viewer"))
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
        },
        set(),
    )
    return _PitrFixture(oss_uploader=oss_uploader, oss_viewer=oss_viewer)


def _validate_env_patch() -> EnvPatchValidator:
    from shared.config.candidate import validate_env_patch

    return validate_env_patch


def _validate_env_patch_for_write() -> Callable[[dict[str, object], set[str]], EnvPatchValidation]:
    from shared.config.candidate import validate_env_patch_for_write

    return validate_env_patch_for_write


def test_oss_patch_without_viewer_credential_is_rejected(
    configured_gcs_pitr_home: _PitrFixture,
) -> None:
    """The four-key OSS transition cannot persist a restore-proof config without a viewer."""
    validate_env_patch = _validate_env_patch()
    errors = validate_env_patch(
        {
            "pitr_store_backend": "oss",
            "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
            "pitr_oss_bucket": "test-bucket",
            "pitr_oss_credentials_file": configured_gcs_pitr_home.oss_uploader,
        },
        set(),
    )
    assert errors
    assert any("AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE" in error for error in errors)


def test_oss_patch_with_distinct_viewer_credential_is_valid(
    configured_gcs_pitr_home: _PitrFixture,
) -> None:
    """A full OSS transition validates when its viewer credential is independent."""
    validate_env_patch = _validate_env_patch()
    assert (
        validate_env_patch(
            {
                "pitr_store_backend": "oss",
                "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
                "pitr_oss_bucket": "test-bucket",
                "pitr_oss_credentials_file": configured_gcs_pitr_home.oss_uploader,
                "pitr_oss_viewer_credentials_file": configured_gcs_pitr_home.oss_viewer,
            },
            set(),
        )
        == []
    )


def test_unrelated_domain_patch_is_valid(configured_gcs_pitr_home: _PitrFixture) -> None:
    """Validation reconstructs only the domain the patch changes."""
    validate_env_patch = _validate_env_patch()
    assert validate_env_patch({"llm_model": "candidate-model"}, set()) == []


def test_unrelated_invalid_domain_does_not_reject_a_candidate(
    configured_gcs_pitr_home: _PitrFixture,
) -> None:
    """A broken sandbox file value cannot poison an independent model candidate."""
    runtime_config.write_fields(
        {
            "exec_timeout_seconds": 1200,
            "exec_node_timeout_seconds": 300,
        },
        set(),
    )

    assert _validate_env_patch()({"llm_model": "candidate-model"}, set()) == []


def test_removal_uses_the_field_default(configured_gcs_pitr_home: _PitrFixture) -> None:
    """Removing restore-proof enablement falls back to its safe false default."""
    validate_env_patch = _validate_env_patch()
    assert validate_env_patch({}, {"pitr_restore_proof_enabled"}) == []


def test_removing_required_field_is_invalid(configured_gcs_pitr_home: _PitrFixture) -> None:
    """A required data-plane value cannot disappear from the persisted candidate."""
    validate_env_patch = _validate_env_patch()
    errors = validate_env_patch({}, {"db_url"})
    assert errors
    assert any("AVA_DB_URL" in error and "Field required" in error for error in errors)


def test_validate_or_raise_joins_candidate_errors(configured_gcs_pitr_home: _PitrFixture) -> None:
    """Non-HTTP callers receive the same alias-safe candidate explanation."""
    from shared.config.candidate import validate_env_patch_or_raise

    with pytest.raises(ValueError, match="AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE"):
        validate_env_patch_or_raise(
            {
                "pitr_store_backend": "oss",
                "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
                "pitr_oss_bucket": "test-bucket",
                "pitr_oss_credentials_file": configured_gcs_pitr_home.oss_uploader,
            },
            set(),
        )


def test_current_invalid_domain_is_rejected_until_the_patch_repairs_it(
    configured_gcs_pitr_home: _PitrFixture,
) -> None:
    """A same-domain edit cannot hide a pre-existing incomplete OSS transition."""
    runtime_config.write_fields(
        {
            "pitr_store_backend": "oss",
            "pitr_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
            "pitr_oss_bucket": "test-bucket",
            "pitr_oss_credentials_file": configured_gcs_pitr_home.oss_uploader,
        },
        set(),
    )
    validate_env_patch = _validate_env_patch()

    errors = validate_env_patch({"pitr_oss_bucket": "replacement-bucket"}, set())

    assert any("AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE" in error for error in errors)
    assert (
        validate_env_patch(
            {"pitr_oss_viewer_credentials_file": configured_gcs_pitr_home.oss_viewer}, set()
        )
        == []
    )


def test_empty_patch_is_trivially_valid(configured_gcs_pitr_home: _PitrFixture) -> None:
    """No touched domain means no candidate reconstruction or rejection."""
    validate_env_patch = _validate_env_patch()
    assert validate_env_patch({}, set()) == []


def test_stale_candidate_digest_cannot_persist_an_invalid_combination(
    configured_gcs_pitr_home: _PitrFixture,
) -> None:
    """A later valid patch must retry after another candidate has changed `.env`."""
    runtime_config.write_fields(
        {
            "pitr_base_backup_enabled": False,
            "pitr_restore_proof_enabled": False,
        },
        set(),
    )
    validate_for_write = _validate_env_patch_for_write()
    disable_pitr = validate_for_write({"pitr_enabled": False}, set())
    enable_base_backup = validate_for_write({"pitr_base_backup_enabled": True}, set())

    assert disable_pitr.errors == []
    assert enable_base_backup.errors == []
    assert disable_pitr.expected_digest == enable_base_backup.expected_digest

    runtime_config.write_fields(
        {"pitr_enabled": False},
        set(),
        expected_digest=disable_pitr.expected_digest,
    )
    with pytest.raises(RuntimeError, match="changed before owned runtime-config write"):
        runtime_config.write_fields(
            {"pitr_base_backup_enabled": True},
            set(),
            expected_digest=enable_base_backup.expected_digest,
        )
