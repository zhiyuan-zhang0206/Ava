"""Credential-file validation for the physical backup plane.

The identity parsers here prove the role separation the PITR plane relies on
(uploader vs viewer-only vs deletion-only) without building any client. Every
helper raises ``ValueError``/``TypeError`` naming the env alias, so a
misprovisioned file fails config load with the exact key to fix. The split
keeps the settings model itself under the per-file line budget.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast


def require_private_regular_file(path: Path | None, alias: str) -> None:
    if path is None or not path.is_absolute():
        raise ValueError(f"{alias} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{alias} must exist") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{alias} must be a non-symlink regular file with mode 0600")


def service_account_identity(path: Path, alias: str) -> tuple[str, str, str]:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
        validate_service_account_payload(raw)
        return (
            str(raw["client_email"]),
            str(raw["project_id"]),
            hashlib.sha256(payload).hexdigest(),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{alias} must contain a service-account identity") from exc


def validate_service_account_payload(raw: object) -> None:
    if not isinstance(raw, dict):
        raise TypeError("service-account payload must be an object")
    payload = cast(dict[str, object], raw)
    if {"type", "client_email", "project_id", "private_key_id"} - set(payload):
        raise ValueError("service-account identity fields are missing")
    if payload["type"] != "service_account":
        raise ValueError("credential is not a service account")


def aliyun_oss_identity(path: Path, alias: str) -> tuple[str, str]:
    """Validate a 0600 Aliyun OSS credential JSON and return its
    (access_key_id, sha256) identity -- the viewer/uploader distinction
    proof for restore drills."""
    require_private_regular_file(path, alias)
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


def reject_shared_delete_identity(
    delete_file: Path | None,
    delete_alias: str,
    peers: tuple[tuple[Path | None, str], ...],
    *,
    parse: Callable[[Path, str], tuple[str, ...]],
    distinct_indexes: tuple[int, ...],
) -> None:
    """The deletion identity must differ from every peer role identity.

    Only the tuple indexes that ARE the identity are compared (email + file
    digest for service accounts; key id + file digest for OSS), so a
    legitimately shared field such as a GCP project id never trips the check.
    A missing delete file is skipped: provisioning order must not break config
    load -- the store factory fails closed at use time instead.
    """
    if delete_file is None or not delete_file.is_file():
        return
    delete_identity = parse(delete_file, delete_alias)
    for peer, peer_alias in peers:
        if peer is None or not Path(peer).is_file():
            continue
        peer_identity = parse(Path(peer), peer_alias)
        if any(delete_identity[index] == peer_identity[index] for index in distinct_indexes):
            raise ValueError(
                f"{delete_alias} must be a distinct deletion-only identity (not {peer_alias})"
            )
