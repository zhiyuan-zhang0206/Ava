from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.config.physical_backup import PhysicalBackupSettings


def _service_account(email: str) -> str:
    return (
        '{"type":"service_account","client_email":"'
        + email
        + '","project_id":"project","private_key_id":"key"}'
    )


def test_pitr_is_disabled_without_credentials_or_cluster_secret() -> None:
    settings = PhysicalBackupSettings()
    assert settings.pitr_enabled is False
    assert settings.pitr_base_backup_enabled is False
    assert settings.pitr_retention_planner_enabled is False
    assert settings.pitr_backup_key_file is None


def test_store_backend_defaults_to_gcs_and_accepts_the_env_alias() -> None:
    settings = PhysicalBackupSettings()
    assert settings.pitr_store_backend == "gcs"
    configured = PhysicalBackupSettings(AVA_PITR_STORE_BACKEND="gcs")
    assert configured.pitr_store_backend == "gcs"


def test_retention_planner_requires_restore_proof_gate() -> None:
    with pytest.raises(ValidationError, match="RETENTION_PLANNER_ENABLED requires"):
        PhysicalBackupSettings(AVA_PITR_RETENTION_PLANNER_ENABLED=True)


def test_enabled_pitr_requires_independent_private_key(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    credentials = tmp_path / "gcs.json"
    credentials.write_text(_service_account("uploader@example.com"))
    credentials.chmod(0o600)
    settings = PhysicalBackupSettings(
        AVA_PITR_ENABLED=True,
        AVA_PITR_GCS_PROJECT="project",
        AVA_PITR_GCS_BUCKET="bucket",
        AVA_PITR_BACKUP_KEY_FILE=key,
        AVA_PITR_GCS_CREDENTIALS_FILE=credentials,
        AVA_PITR_BACKUP_KEY_ID="prod-v1",
        AVA_PITR_REPLICATION_DB_URL="postgresql://backup:secret@localhost:5433/postgres",
    )
    assert settings.pitr_backup_key_file == key


def test_base_candidates_require_wal_pitr_to_be_enabled() -> None:
    with pytest.raises(ValidationError, match="BASE_BACKUP_ENABLED requires"):
        PhysicalBackupSettings(AVA_PITR_BASE_BACKUP_ENABLED=True)


def test_replication_url_rejects_remote_postgres() -> None:
    with pytest.raises(ValidationError, match="must target loopback"):
        PhysicalBackupSettings(
            AVA_PITR_REPLICATION_DB_URL="postgresql://backup:secret@db.example:5432/postgres"
        )


def test_base_candidates_accept_the_fully_validated_pitr_contract(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    credentials = tmp_path / "gcs.json"
    credentials.write_text("{}")
    credentials.chmod(0o600)
    settings = PhysicalBackupSettings(
        AVA_PITR_ENABLED=True,
        AVA_PITR_BASE_BACKUP_ENABLED=True,
        AVA_PITR_GCS_PROJECT="project",
        AVA_PITR_GCS_BUCKET="bucket",
        AVA_PITR_BACKUP_KEY_FILE=key,
        AVA_PITR_GCS_CREDENTIALS_FILE=credentials,
        AVA_PITR_BACKUP_KEY_ID="prod-v1",
        AVA_PITR_REPLICATION_DB_URL="postgresql://backup:secret@localhost:5433/postgres",
    )
    assert settings.pitr_base_backup_enabled is True


def test_restore_proof_requires_a_distinct_viewer_credential(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    credentials = tmp_path / "gcs.json"
    credentials.write_text(_service_account("uploader@example.com"))
    credentials.chmod(0o600)
    with pytest.raises(ValidationError, match="distinct viewer-only credential"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED=True,
            AVA_PITR_BASE_BACKUP_ENABLED=True,
            AVA_PITR_RESTORE_PROOF_ENABLED=True,
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE=key,
            AVA_PITR_GCS_CREDENTIALS_FILE=credentials,
            AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE=credentials,
            AVA_PITR_BACKUP_KEY_ID="prod-v1",
            AVA_PITR_REPLICATION_DB_URL=("postgresql://backup:secret@localhost:5433/postgres"),
        )

    viewer = tmp_path / "viewer.json"
    viewer.write_text(_service_account("viewer@example.com"))
    viewer.chmod(0o600)
    settings = PhysicalBackupSettings(
        AVA_PITR_ENABLED=True,
        AVA_PITR_BASE_BACKUP_ENABLED=True,
        AVA_PITR_RESTORE_PROOF_ENABLED=True,
        AVA_PITR_GCS_PROJECT="project",
        AVA_PITR_GCS_BUCKET="bucket",
        AVA_PITR_BACKUP_KEY_FILE=key,
        AVA_PITR_GCS_CREDENTIALS_FILE=credentials,
        AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE=viewer,
        AVA_PITR_BACKUP_KEY_ID="prod-v1",
        AVA_PITR_REPLICATION_DB_URL="postgresql://backup:secret@localhost:5433/postgres",
    )
    assert settings.pitr_restore_proof_enabled is True


def test_enabled_pitr_rejects_empty_or_overexposed_key(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.touch(mode=0o644)
    with pytest.raises(ValidationError, match="32-byte, non-symlink regular file with mode 0600"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED=True,
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE=key,
            AVA_PITR_GCS_CREDENTIALS_FILE=key,
            AVA_PITR_BACKUP_KEY_ID="prod-v1",
            AVA_PITR_REPLICATION_DB_URL="postgresql://backup:secret@localhost:5433/postgres",
        )


def test_enabled_pitr_rejects_relative_key_path() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED=True,
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE=Path("backup.key"),
            AVA_PITR_GCS_CREDENTIALS_FILE=Path("gcs.json"),
            AVA_PITR_BACKUP_KEY_ID="prod-v1",
        )


def test_enabled_pitr_rejects_overexposed_credentials(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    credentials = tmp_path / "gcs.json"
    credentials.write_text("{}")
    credentials.chmod(0o644)
    with pytest.raises(ValidationError, match=r"GCS_CREDENTIALS_FILE.*mode 0600"):
        PhysicalBackupSettings(
            AVA_PITR_ENABLED=True,
            AVA_PITR_GCS_PROJECT="project",
            AVA_PITR_GCS_BUCKET="bucket",
            AVA_PITR_BACKUP_KEY_FILE=key,
            AVA_PITR_GCS_CREDENTIALS_FILE=credentials,
            AVA_PITR_BACKUP_KEY_ID="prod-v1",
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


def test_replication_hba_lines_render_the_role_from_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings
    from shared.config.physical_backup import pitr_replication_hba_lines

    monkeypatch.setattr(
        settings.physical_backup,
        "pitr_replication_db_url",
        "postgresql://ava_pitr_repl:pw@127.0.0.1:5433/ava_main",
    )
    assert pitr_replication_hba_lines() == [
        "host replication ava_pitr_repl 127.0.0.1/32 scram-sha-256",
        "host replication ava_pitr_repl ::1/128 scram-sha-256",
    ]


def test_replication_hba_lines_empty_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings
    from shared.config.physical_backup import pitr_replication_hba_lines

    monkeypatch.setattr(settings.physical_backup, "pitr_replication_db_url", None)
    assert pitr_replication_hba_lines() == []


def test_replication_hba_lines_empty_when_url_has_no_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty user must yield no rows: `host replication  127.0.0.1/32 ...`
    would be an invalid line and PG17 rejects the whole file (QA #1096 P2)."""
    from shared.config import settings
    from shared.config.physical_backup import pitr_replication_hba_lines

    monkeypatch.setattr(
        settings.physical_backup,
        "pitr_replication_db_url",
        "postgresql://:pw@127.0.0.1:5433/ava_main",
    )
    assert pitr_replication_hba_lines() == []
