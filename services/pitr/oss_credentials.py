"""Aliyun OSS access-key identity for the PITR store group.

OSS authenticates with a static RAM AccessKey pair (no token refresh, unlike
the Baidu OAuth exchange). The credentials file carries exactly the two
secrets; the endpoint and bucket are non-secret settings, so they live in
``PhysicalBackupSettings`` rather than in the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import oss2


@dataclass(frozen=True)
class AliyunCredentials:
    """One RAM AccessKey pair loaded from a validated credentials file."""

    access_key_id: str
    access_key_secret: str


def aliyun_credentials(path: str | Path) -> AliyunCredentials:
    """Load and validate a 0600 OSS credentials JSON file.

    The file shape is ``{"access_key_id": ..., "access_key_secret": ...}``;
    both values must be non-empty strings. Anything else fails fast —
    a malformed credential must never reach the network layer.
    """
    try:
        payload: object = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("OSS credentials file is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("OSS credentials payload must be an object")
    raw = cast(dict[str, object], payload)
    access_key_id = raw.get("access_key_id")
    access_key_secret = raw.get("access_key_secret")
    if (
        not isinstance(access_key_id, str)
        or not isinstance(access_key_secret, str)
        or not access_key_id
        or not access_key_secret
    ):
        raise ValueError(
            "OSS credentials must carry a non-empty access_key_id and access_key_secret"
        )
    return AliyunCredentials(access_key_id, access_key_secret)


def aliyun_auth(credentials: AliyunCredentials) -> oss2.Auth:
    """Build the oss2 signing Auth for one AccessKey pair."""
    return oss2.Auth(credentials.access_key_id, credentials.access_key_secret)


def open_oss_bucket(
    *,
    endpoint: str,
    bucket: str,
    credentials_file: str | Path,
    timeout_seconds: float = 300.0,
) -> oss2.Bucket:
    """Open the OSS Bucket connection for one credential file.

    ``endpoint`` is the region endpoint (e.g.
    ``https://oss-cn-shanghai.aliyuncs.com``); trailing slashes are trimmed.
    The connect timeout covers the control-plane setup; per-request retry
    discipline lives in the adapter (a store error maps to the transient /
    permanent boundary, and the uploader loop owns retries).
    """
    credentials = aliyun_credentials(credentials_file)
    return oss2.Bucket(
        aliyun_auth(credentials),
        endpoint.rstrip("/"),
        bucket,
        connect_timeout=int(timeout_seconds),
    )
