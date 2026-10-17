# meiva:ref base/config/docs/to-user-limits.meiva.okf.md#to-user-read-limits
from __future__ import annotations

from typing import Any

from pydantic import Field

from base.config.base import EnvSettings

_TO_USER_FIELD: Any = {
    "restart_required": "all",
    "writable": True,
    "sensitive": False,
    "scope": "cluster-pinned",
}


class ToUserSettings(EnvSettings):
    max_document_bytes: int = Field(
        default=1024 * 1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_DOCUMENT_BYTES",
        description=(
            "Largest Markdown panel document the reader accepts, in bytes. A guard "
            "against reading the wrong file as a panel document, not a writing "
            "bound: the 1500-word soft limit shapes the panel "
            "(`to_user_soft_limit_words`) and the largest document in the fleet is "
            "about 5 KiB. The first-version bound was 256 KiB; 1 MiB keeps even "
            "extreme reports readable."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_references: int = Field(
        default=1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_REFERENCES",
        description=(
            "Most local references one panel document may carry. Each reference "
            "costs one stat/version probe while the document is read; the bound "
            "caps a pathological document rather than normal authoring — real "
            "panels carry a handful. The first-version bound was 256."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_chain_depth: int = Field(
        default=16,
        gt=0,
        alias="MEIVA_TO_USER_MAX_CHAIN_DEPTH",
        description=(
            "Longest reference chain a resource read may follow, across nested "
            "Markdown documents. Real report chains nest two or three documents; "
            "the bound stops a runaway or circular chain. The first-version bound "
            "was 8."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_resource_bytes: int = Field(
        default=64 * 1024 * 1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_RESOURCE_BYTES",
        description=(
            "Largest single referenced file served through the authenticated "
            "resource endpoint, in bytes. Both ends enforce it: the reading "
            "machine refuses larger files and the gateway aborts a larger stream "
            "with 413. Sized for evidence-grade attachments — logs and data files "
            "in workspaces already reach 30-40 MiB — and since the gateway buffers "
            "the body in memory, this is also the per-request memory bound. The "
            "first-version bound was 20 MiB."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )
