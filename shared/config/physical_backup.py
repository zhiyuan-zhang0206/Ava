"""Physical Postgres backup and PITR configuration."""

from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath

from pydantic import Field, model_validator

from shared.config._base import EnvSettings

_SAFE_PREFIX_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PhysicalBackupSettings(EnvSettings):
    """Cluster-pinned settings for the disabled-by-default physical backup plane."""

    pitr_enabled: bool = Field(
        default=False,
        alias="AVA_PITR_ENABLED",
        description="Enable physical backup wiring. This foundation release never enables Postgres archiving.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_gcs_project: str = Field(
        default="",
        alias="AVA_PITR_GCS_PROJECT",
        description="GCS project for physical backup objects.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_gcs_bucket: str = Field(
        default="",
        alias="AVA_PITR_GCS_BUCKET",
        description="GCS bucket for physical backup objects.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_gcs_prefix: str = Field(
        default="ava-pitr",
        alias="AVA_PITR_GCS_PREFIX",
        description="Relative object prefix; traversal and empty path components are rejected.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_archive_timeout_seconds: int = Field(
        default=60,
        ge=30,
        le=3600,
        alias="AVA_PITR_ARCHIVE_TIMEOUT_SECONDS",
        description="Maximum age of a partially filled WAL segment before PostgreSQL requests archiving.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_spool_warn_bytes: int = Field(
        default=1181116006,
        ge=16 * 1024 * 1024,
        alias="AVA_PITR_SPOOL_WARN_BYTES",
        description="Warn when unacknowledged local WAL reaches roughly one production hour.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_spool_hard_bytes: int = Field(
        default=2362232013,
        ge=32 * 1024 * 1024,
        alias="AVA_PITR_SPOOL_HARD_BYTES",
        description="Refuse new spool publications at this bound; PostgreSQL then retains WAL in pg_wal.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_unacked_warn_seconds: int = Field(
        default=3600,
        ge=60,
        alias="AVA_PITR_UNACKED_WARN_SECONDS",
        description="Degrade health when the oldest local segment lacks remote ACK for this long.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_unacked_critical_seconds: int = Field(
        default=7200,
        ge=120,
        alias="AVA_PITR_UNACKED_CRITICAL_SECONDS",
        description="Mark health critical when remote ACK lag reaches this age.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_retained_weekly_chains: int = Field(
        default=2,
        ge=2,
        le=8,
        alias="AVA_PITR_RETAINED_WEEKLY_CHAINS",
        description="Verified weekly base-backup chains retained remotely.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_backup_key_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_BACKUP_KEY_FILE",
        description="0600 file holding the independent physical-backup key; never the cluster secret.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> PhysicalBackupSettings:
        raw_prefix = self.pitr_gcs_prefix
        if "//" in raw_prefix or any(part in {"", ".", ".."} for part in raw_prefix.split("/")):
            raise ValueError("AVA_PITR_GCS_PREFIX must be a safe relative object prefix")
        prefix = PurePosixPath(raw_prefix)
        parts = prefix.parts
        if (
            not parts
            or prefix.is_absolute()
            or any(
                part in {"", ".", ".."} or not _SAFE_PREFIX_PART.fullmatch(part) for part in parts
            )
        ):
            raise ValueError("AVA_PITR_GCS_PREFIX must be a safe relative object prefix")
        if self.pitr_spool_warn_bytes >= self.pitr_spool_hard_bytes:
            raise ValueError("AVA_PITR_SPOOL_WARN_BYTES must be below AVA_PITR_SPOOL_HARD_BYTES")
        if self.pitr_unacked_warn_seconds >= self.pitr_unacked_critical_seconds:
            raise ValueError(
                "AVA_PITR_UNACKED_WARN_SECONDS must be below AVA_PITR_UNACKED_CRITICAL_SECONDS"
            )
        if self.pitr_enabled and not (
            self.pitr_gcs_project and self.pitr_gcs_bucket and self.pitr_backup_key_file
        ):
            raise ValueError(
                "PITR enabled requires AVA_PITR_GCS_PROJECT, AVA_PITR_GCS_BUCKET, "
                "and AVA_PITR_BACKUP_KEY_FILE"
            )
        if self.pitr_enabled:
            key_file = self.pitr_backup_key_file
            if key_file is None:
                raise ValueError("AVA_PITR_BACKUP_KEY_FILE is required")
            if not key_file.is_absolute():
                raise ValueError("AVA_PITR_BACKUP_KEY_FILE must be an absolute path")
            try:
                info = key_file.lstat()
            except OSError as exc:
                raise ValueError("AVA_PITR_BACKUP_KEY_FILE must exist") from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or key_file.is_symlink()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size == 0
            ):
                raise ValueError(
                    "AVA_PITR_BACKUP_KEY_FILE must be a non-empty, non-symlink regular file with mode 0600"
                )
        return self
