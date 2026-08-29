from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.config.physical_backup import PhysicalBackupSettings


def test_pitr_is_disabled_without_credentials_or_cluster_secret() -> None:
    settings = PhysicalBackupSettings()
    assert settings.pitr_enabled is False
    assert settings.pitr_backup_key_file is None


def test_enabled_pitr_requires_independent_private_key(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_text("independent")
    key.chmod(0o600)
    settings = PhysicalBackupSettings(
        AVA_PITR_ENABLED="true",
        AVA_PITR_GCS_PROJECT="project",
        AVA_PITR_GCS_BUCKET="bucket",
        AVA_PITR_BACKUP_KEY_FILE=str(key),
    )
    assert settings.pitr_backup_key_file == key


def test_enabled_pitr_rejects_empty_or_overexposed_key(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.touch(mode=0o644)
    with pytest.raises(ValidationError, match="non-empty, non-symlink regular file with mode 0600"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED="true",
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE=str(key),
        )


def test_enabled_pitr_rejects_relative_key_path() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED="true",
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE="backup.key",
        )


@pytest.mark.parametrize("prefix", ["/absolute", "../escape", "a//b", "a/./b"])
def test_prefix_rejects_unsafe_paths(prefix: str) -> None:
    with pytest.raises(ValidationError, match="safe relative object prefix"):
        PhysicalBackupSettings(AVA_PITR_GCS_PREFIX=prefix)


def test_warn_threshold_must_precede_hard_threshold() -> None:
    with pytest.raises(ValidationError, match="must be below"):
        PhysicalBackupSettings(
            AVA_PITR_SPOOL_WARN_BYTES=32 * 1024 * 1024,
            AVA_PITR_SPOOL_HARD_BYTES=32 * 1024 * 1024,
        )


def test_unacked_warning_must_precede_critical_age() -> None:
    with pytest.raises(ValidationError, match="UNACKED_WARN_SECONDS must be below"):
        PhysicalBackupSettings(
            AVA_PITR_UNACKED_WARN_SECONDS=7200,
            AVA_PITR_UNACKED_CRITICAL_SECONDS=7200,
        )
