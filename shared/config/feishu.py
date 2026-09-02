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
            "restart_required": "gateway",
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
            "restart_required": "gateway",
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
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    feishu_poll_interval_seconds: float = Field(
        default=0.5,
        alias="AVA_FEISHU_POLL_INTERVAL_SECONDS",
        description="Polling interval (seconds) for the p2p chat fallback. The Feishu platform does not deliver im.message.receive_v1 for this app (diagnosed 2026-09-01), so the adapter polls the known p2p chat via ListMessage and feeds new user texts like WS events. 0 disables polling (WS-only).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    feishu_poll_chat_id: str = Field(
        default="",
        alias="AVA_FEISHU_POLL_CHAT_ID",
        description="Bootstrap p2p chat id (oc_...) to poll. Needed to catch the FIRST user message before any outbound send resolved the chat; chats are also discovered automatically from outbound send responses.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
