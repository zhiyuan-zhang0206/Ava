"""Shared PITR test fixtures (backend credential evidence shapes)."""

from __future__ import annotations


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
