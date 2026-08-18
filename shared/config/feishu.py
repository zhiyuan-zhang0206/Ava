"""Feishu config — FeishuSettings.

Credentials for the Ava IM Bridge feishu adapter (a Feishu enterprise
self-built app you register). The adapter reads these; empty values mean the feishu
channel is unavailable and the daemon logs "skipped".
"""

from __future__ import annotations

from pydantic import Field

from shared.config._base import EnvSettings


class FeishuSettings(EnvSettings):
    feishu_app_id: str = Field(
        default="",
        alias="AVA_FEISHU_APP_ID",
        description="Feishu app id (cli_...) of the IM Bridge self-built app. Empty = feishu channel unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    feishu_app_secret: str = Field(
        default="",
        alias="AVA_FEISHU_APP_SECRET",
        description="Feishu app secret of the IM Bridge app. Empty = feishu channel unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    feishu_rest_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_FEISHU_REST_TIMEOUT_SECONDS",
        description="Feishu REST API timeout (seconds) for outbound sends. The lark-oapi SDK's own default is 30s; pinning it explicitly keeps a hung Feishu line from parking an IM outbound at whatever the SDK version happens to default to (task #698 G6).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
