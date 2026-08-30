"""Physical Postgres backup and PITR configuration."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from shared.config._base import EnvSettings

_SAFE_PREFIX_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _require_private_regular_file(path: Path | None, alias: str) -> None:
    if path is None or not path.is_absolute():
        raise ValueError(f"{alias} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{alias} must exist") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{alias} must be a non-symlink regular file with mode 0600")


def _service_account_identity(path: Path, alias: str) -> tuple[str, str, str]:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
        _validate_service_account_payload(raw)
        return (
            str(raw["client_email"]),
            str(raw["project_id"]),
            hashlib.sha256(payload).hexdigest(),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{alias} must contain a service-account identity") from exc


def _validate_service_account_payload(raw: object) -> None:
    if not isinstance(raw, dict):
        raise TypeError("service-account payload must be an object")
    payload = cast(dict[str, object], raw)
    if {"type", "client_email", "project_id", "private_key_id"} - set(payload):
        raise ValueError("service-account identity fields are missing")
    if payload["type"] != "service_account":
        raise ValueError("credential is not a service account")


def _aliyun_oss_identity(path: Path, alias: str) -> tuple[str, str]:
    """Validate a 0600 Aliyun OSS credential JSON and return its
    (access_key_id, sha256) identity — the viewer/uploader distinction
    proof for restore drills."""
    _require_private_regular_file(path, alias)
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{alias} must contain an Aliyun OSS identity") from exc
    if not isinstance(raw, dict):
        raise TypeError("OSS credential payload must be an object")
    credentials = cast(dict[str, object], raw)
    key_id = credentials.get("access_key_id")
    key_secret = credentials.get("access_key_secret")
    if not isinstance(key_id, str) or not isinstance(key_secret, str):
        raise TypeError("OSS credential identity fields are missing")
    if not key_id or not key_secret:
        raise ValueError("OSS credential identity fields must be non-empty")
    return key_id, hashlib.sha256(payload).hexdigest()


class PhysicalBackupSettings(EnvSettings):
    """Cluster-pinned settings for the disabled-by-default physical backup plane."""

    pitr_enabled: bool = Field(
        default=False,
        alias="AVA_PITR_ENABLED",
        description="Enable physical backup wiring. This foundation release never enables Postgres archiving. Gateway-local enablement: never served by bootstrap to agent-runners.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_base_backup_enabled: bool = Field(
        default=False,
        alias="AVA_PITR_BASE_BACKUP_ENABLED",
        description="Enable weekly unprotected physical base-backup candidates. Gateway-local enablement: never served by bootstrap to agent-runners.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_restore_proof_enabled: bool = Field(
        default=False,
        alias="AVA_PITR_RESTORE_PROOF_ENABLED",
        description="Enable generation-pinned isolated restore drills for base candidates. Gateway-local enablement: never served by bootstrap to agent-runners.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_retention_planner_enabled: bool = Field(
        default=False,
        alias="AVA_PITR_RETENTION_PLANNER_ENABLED",
        description="Enable local dry-run PITR retention planning; this never deletes objects. Gateway-local enablement: never served by bootstrap to agent-runners.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_store_backend: str = Field(
        default="gcs",
        alias="AVA_PITR_STORE_BACKEND",
        description=(
            "Object-store backend for PITR uploads and restores — 'gcs' (default). "
            "New backends land behind the same switch; an unrecognized value "
            "fails fast at store construction (it never silently falls back to "
            "GCS). Every PITR daemon reads it, so switching is one env var + a "
            "restart."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_baidu_app_root: str = Field(
        default="",
        alias="AVA_PITR_BAIDU_APP_ROOT",
        description=(
            "Baidu Netdisk app data directory (e.g. /apps/<appname>); the PCS "
            "API forbids writes outside this boundary. Required when the "
            "store backend is baidu."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_baidu_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_BAIDU_CREDENTIALS_FILE",
        description=(
            "0600 JSON holding the Baidu open-platform app identity: app_key, "
            "secret_key, and the durable refresh_token. Never the cluster secret."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
        },
    )
    pitr_baidu_token_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_BAIDU_TOKEN_FILE",
        description=(
            "0600 JSON managed by the Baidu token manager: the access token "
            "pair. May not exist yet (created on first refresh); when present "
            "it must be a 0600 non-symlink regular file."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
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
    pitr_oss_endpoint: str = Field(
        default="",
        alias="AVA_PITR_OSS_ENDPOINT",
        description=(
            "Aliyun OSS region endpoint (e.g. https://oss-cn-shanghai.aliyuncs.com). "
            "Required when the store backend is oss."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_oss_bucket: str = Field(
        default="",
        alias="AVA_PITR_OSS_BUCKET",
        description=(
            "Aliyun OSS bucket for physical-backup objects. Required when the store backend is oss."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    pitr_oss_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_OSS_CREDENTIALS_FILE",
        description=(
            "0600 JSON holding the Aliyun OSS RAM AccessKey pair "
            "(access_key_id, access_key_secret) used by the PITR uploader "
            "roles. Never the cluster secret."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
        },
    )
    pitr_oss_viewer_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE",
        description=(
            "0600 JSON holding a viewer-only Aliyun OSS RAM AccessKey pair "
            "for restore drills and retention inventory. Required with a "
            "distinct identity when the store backend is oss and restore "
            "proof is enabled."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
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
    pitr_gcs_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_GCS_CREDENTIALS_FILE",
        description="0600 service-account JSON used only by the PITR uploader.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_restore_gcs_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE",
        description="0600 viewer-only service-account JSON used by restore drills.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_backup_key_id: str = Field(
        default="",
        alias="AVA_PITR_BACKUP_KEY_ID",
        description="Non-secret identifier embedded in encrypted object metadata.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    pitr_replication_db_url: str = Field(
        default="",
        alias="AVA_PITR_REPLICATION_DB_URL",
        description="Local least-privilege REPLICATION role URL used only by pg_basebackup.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> PhysicalBackupSettings:  # noqa: PLR0915
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
        if self.pitr_enabled and not (self.pitr_backup_key_file and self.pitr_backup_key_id):
            raise ValueError(
                "PITR enabled requires AVA_PITR_BACKUP_KEY_FILE and AVA_PITR_BACKUP_KEY_ID"
            )
        if (
            self.pitr_enabled
            and self.pitr_store_backend == "gcs"
            and not (
                self.pitr_gcs_project and self.pitr_gcs_bucket and self.pitr_gcs_credentials_file
            )
        ):
            raise ValueError(
                "the gcs store backend requires AVA_PITR_GCS_PROJECT, AVA_PITR_GCS_BUCKET, "
                "and AVA_PITR_GCS_CREDENTIALS_FILE"
            )
        if (
            self.pitr_enabled
            and self.pitr_store_backend == "baidu"
            and not (self.pitr_baidu_app_root and self.pitr_baidu_credentials_file)
        ):
            raise ValueError(
                "the baidu store backend requires AVA_PITR_BAIDU_APP_ROOT and "
                "AVA_PITR_BAIDU_CREDENTIALS_FILE"
            )
        if self.pitr_enabled and self.pitr_store_backend == "oss":
            if not (self.pitr_oss_endpoint and self.pitr_oss_bucket):
                raise ValueError(
                    "the oss store backend requires AVA_PITR_OSS_ENDPOINT and AVA_PITR_OSS_BUCKET"
                )
            parsed_endpoint = urlsplit(self.pitr_oss_endpoint)
            if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
                raise ValueError("AVA_PITR_OSS_ENDPOINT must be an http(s) region endpoint URL")
        if (
            self.pitr_enabled
            and self.pitr_store_backend == "oss"
            and self.pitr_oss_credentials_file is None
        ):
            raise ValueError("the oss store backend requires AVA_PITR_OSS_CREDENTIALS_FILE")
        if self.pitr_base_backup_enabled and not self.pitr_enabled:
            raise ValueError("AVA_PITR_BASE_BACKUP_ENABLED requires AVA_PITR_ENABLED")
        if self.pitr_restore_proof_enabled and not self.pitr_base_backup_enabled:
            raise ValueError("AVA_PITR_RESTORE_PROOF_ENABLED requires AVA_PITR_BASE_BACKUP_ENABLED")
        if self.pitr_retention_planner_enabled and not self.pitr_restore_proof_enabled:
            raise ValueError(
                "AVA_PITR_RETENTION_PLANNER_ENABLED requires AVA_PITR_RESTORE_PROOF_ENABLED"
            )
        if self.pitr_base_backup_enabled and not self.pitr_replication_db_url:
            raise ValueError("AVA_PITR_BASE_BACKUP_ENABLED requires AVA_PITR_REPLICATION_DB_URL")
        if self.pitr_replication_db_url:
            parsed_replication = urlsplit(self.pitr_replication_db_url)
            if parsed_replication.scheme not in {
                "postgres",
                "postgresql",
            } or parsed_replication.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("AVA_PITR_REPLICATION_DB_URL must target loopback PostgreSQL")
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
                or info.st_size != 32
            ):
                raise ValueError(
                    "AVA_PITR_BACKUP_KEY_FILE must be a 32-byte, non-symlink regular file with mode 0600"
                )
            if self.pitr_store_backend == "gcs":
                _require_private_regular_file(
                    self.pitr_gcs_credentials_file, "AVA_PITR_GCS_CREDENTIALS_FILE"
                )
            if self.pitr_store_backend == "baidu":
                _require_private_regular_file(
                    self.pitr_baidu_credentials_file, "AVA_PITR_BAIDU_CREDENTIALS_FILE"
                )
                token_file = self.pitr_baidu_token_file
                if token_file is None or not token_file.is_absolute():
                    raise ValueError("AVA_PITR_BAIDU_TOKEN_FILE must be an absolute path")
                if token_file.exists():
                    _require_private_regular_file(token_file, "AVA_PITR_BAIDU_TOKEN_FILE")
            if self.pitr_store_backend == "oss":
                if self.pitr_oss_credentials_file is None:
                    raise ValueError("AVA_PITR_OSS_CREDENTIALS_FILE is required")
                _aliyun_oss_identity(
                    self.pitr_oss_credentials_file, "AVA_PITR_OSS_CREDENTIALS_FILE"
                )
            if self.pitr_restore_proof_enabled and self.pitr_store_backend == "gcs":
                _require_private_regular_file(
                    self.pitr_restore_gcs_credentials_file,
                    "AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE",
                )
                uploader = self.pitr_gcs_credentials_file
                viewer = self.pitr_restore_gcs_credentials_file
                if uploader is None or viewer is None:
                    raise ValueError(
                        "restore proof requires separate uploader and viewer credentials"
                    )
                uploader_info = uploader.stat()
                viewer_info = viewer.stat()
                if (uploader_info.st_dev, uploader_info.st_ino) == (
                    viewer_info.st_dev,
                    viewer_info.st_ino,
                ):
                    raise ValueError(
                        "AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE must be a distinct viewer-only credential"
                    )
                uploader_identity = _service_account_identity(
                    uploader, "AVA_PITR_GCS_CREDENTIALS_FILE"
                )
                viewer_identity = _service_account_identity(
                    viewer, "AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE"
                )
                if (
                    uploader_identity[1] != self.pitr_gcs_project
                    or viewer_identity[1] != self.pitr_gcs_project
                ):
                    raise ValueError("PITR service-account project must match AVA_PITR_GCS_PROJECT")
                if (
                    uploader_identity[0] == viewer_identity[0]
                    or uploader_identity[2] == viewer_identity[2]
                ):
                    raise ValueError(
                        "restore proof requires a distinct viewer-only service-account identity"
                    )
            if self.pitr_restore_proof_enabled and self.pitr_store_backend == "oss":
                viewer = self.pitr_oss_viewer_credentials_file
                if viewer is None:
                    raise ValueError("restore proof requires a distinct viewer-only OSS credential")
                uploader = self.pitr_oss_credentials_file
                if uploader is None:
                    raise ValueError("AVA_PITR_OSS_CREDENTIALS_FILE is required")
                uploader_info = uploader.stat()
                viewer_info = viewer.stat()
                if (uploader_info.st_dev, uploader_info.st_ino) == (
                    viewer_info.st_dev,
                    viewer_info.st_ino,
                ):
                    raise ValueError(
                        "AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE must be a distinct "
                        "viewer-only credential"
                    )
                uploader_identity = _aliyun_oss_identity(uploader, "AVA_PITR_OSS_CREDENTIALS_FILE")
                viewer_identity = _aliyun_oss_identity(
                    viewer, "AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE"
                )
                if (
                    uploader_identity[0] == viewer_identity[0]
                    or uploader_identity[1] == viewer_identity[1]
                ):
                    raise ValueError("restore proof requires a distinct viewer-only OSS identity")
        return self


def pitr_replication_hba_lines() -> list[str]:
    """Loopback pg_hba rows for PITR's physical replication connection, or []
    when PITR is not configured. pg_basebackup dials dbname=`replication`,
    which matches only the literal `replication` keyword — `all` never covers
    it (2026-08-30 activation died exactly here). A malformed URL yields no
    rows; the activation preflight fails closed later with the real reason."""
    from psycopg.conninfo import conninfo_to_dict

    from shared.config import settings

    url = settings.physical_backup.pitr_replication_db_url
    if not url:
        return []
    try:
        role = str(conninfo_to_dict(url).get("user") or "")
    except Exception:
        return []
    if not role:
        # An empty user would render `host replication  127.0.0.1/32 ...` —
        # an invalid line that makes PG17 reject the WHOLE file (QA #1096 P2).
        return []
    return [
        f"host replication {role} 127.0.0.1/32 scram-sha-256",
        f"host replication {role} ::1/128 scram-sha-256",
    ]
