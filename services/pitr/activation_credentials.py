# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Activation credential identity + read-access probes per backend.

The activation readiness path proves the store credentials without any
synthetic write: the GCS viewer lists one bucket page, the Baidu token
lists one app-root page. Both are backend-specific, so they live here
instead of the CLI orchestrator (which stays a thin dispatcher).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from google.cloud import storage
from google.oauth2 import service_account

from shared.config import settings


def credential_identity(path: Path) -> tuple[str, str, str]:
    raw_value: object = json.loads(path.read_bytes())
    if not isinstance(raw_value, dict):
        raise TypeError(f"{path} is not a service-account credential")
    value = cast(dict[str, object], raw_value)
    if value.get("type") != "service_account":
        raise RuntimeError(f"{path} is not a service-account credential")
    fields: list[str] = []
    for name in ("client_email", "project_id", "private_key_id"):
        field = value[name]
        if not isinstance(field, str) or not field:
            raise RuntimeError(f"{path} service-account {name} is missing")
        fields.append(field)
    return fields[0], fields[1], fields[2]


def credential_app_key(path: Path) -> str:
    from services.pitr.baidu_token import BaiduCredentials

    return BaiduCredentials(path).app_key


def probe_bucket_read_access(credentials_path: Path) -> str:
    credentials = service_account.Credentials.from_service_account_file(str(credentials_path))
    client = storage.Client(
        project=settings.physical_backup.pitr_gcs_project, credentials=credentials
    )
    bucket_name = settings.physical_backup.pitr_gcs_bucket
    # objectViewer includes list/read, but deliberately not storage.buckets.get.
    # Consuming one result authenticates the viewer against the configured bucket
    # without creating a synthetic probe object or deleting retained evidence.
    next(iter(client.list_blobs(bucket_name, max_results=1, timeout=30)), None)
    return bucket_name


def probe_baidu_read_access(credentials_path: Path) -> str:
    from services.pitr.baidu_pcs import PcsClient
    from services.pitr.baidu_token import BaiduCredentials, BaiduTokenManager

    config = settings.physical_backup
    token_file = config.pitr_baidu_token_file
    if token_file is None or not token_file.is_absolute():
        raise RuntimeError("AVA_PITR_BAIDU_TOKEN_FILE must be an absolute path")
    credentials = BaiduCredentials(credentials_path)
    manager = BaiduTokenManager(credentials, token_file)
    client = PcsClient(manager.get_access_token(), timeout=30)
    # One page of the app root authenticates the token and proves read scope
    # without creating a synthetic probe object or deleting retained evidence.
    next(iter(client.list_dir(config.pitr_baidu_app_root.rstrip("/"), limit=1)), None)
    return config.pitr_baidu_app_root
