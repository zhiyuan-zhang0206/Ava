"""Shared PITR test fixtures (backend credential evidence shapes, the deploy-lease stub)."""

from __future__ import annotations

import pytest

from shared.deploy.state import cluster_lock


def stub_update_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let an activation take and release the cluster deploy lease without a database."""
    monkeypatch.setattr(cluster_lock, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(cluster_lock, "release_update_lock", lambda *_a, **_kw: None)


def baidu_credential_evidence() -> dict[str, str]:
    """The frozen credential evidence a Baidu-backend activation carries."""
    return {
        "backend": "baidu",
        "uploader_identity": "app-key",
        "viewer_identity": "app-key",
        "store_target": "/apps/ava/ava-pitr",
        "object_prefix": "pitr",
        "backup_key_id": "key",
        "backup_key_sha256": "0" * 64,
    }


def oss_credential_evidence() -> dict[str, str]:
    """The frozen credential evidence an OSS-backend activation carries."""
    return {
        "backend": "oss",
        "uploader_identity": "ak-id-uploader",
        "viewer_identity": "ak-id-viewer",
        "store_target": "ava-pitr-prod",
        "object_prefix": "pitr",
        "backup_key_id": "key",
        "backup_key_sha256": "0" * 64,
    }
