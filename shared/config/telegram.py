"""Telegram config — TelegramSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.

Telegram is not a framework service — the `telegram` skill (and the cluster
health probe's owner-alert) read these two values and POST straight to the Bot
API. See decisions/2026-07-22-telegram-out-of-core.md.
"""

from __future__ import annotations

from pydantic import Field

from shared.config._base import EnvSettings


class TelegramSettings(EnvSettings):
    telegram_bot_token: str = Field(
        default="",
        alias="AVA_TELEGRAM_BOT_TOKEN",
        description="Telegram bot token (from BotFather). Empty = telegram push is unavailable (the skill falls back to a Web UI reply).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    telegram_owner_id: int = Field(
        default=0,
        alias="AVA_TELEGRAM_OWNER_ID",
        description="Telegram owner's private-chat id, where every push is sent. 0 = no owner = telegram push unavailable.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telegram_poll_timeout_seconds: int = Field(
        default=50,
        alias="AVA_TELEGRAM_POLL_TIMEOUT_SECONDS",
        description="getUpdates long-poll window (seconds); Telegram caps it around 50s. The HTTP read timeout rides 10s above it (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telegram_reconnect_base_delay_seconds: float = Field(
        default=1.0,
        alias="AVA_TELEGRAM_RECONNECT_BASE_DELAY_SECONDS",
        description="First reconnect delay (seconds) after a failed Telegram poll; the delay doubles per failure (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telegram_reconnect_max_delay_seconds: float = Field(
        default=60.0,
        alias="AVA_TELEGRAM_RECONNECT_MAX_DELAY_SECONDS",
        description="Reconnect delay ceiling (seconds) for the Telegram poll loop's exponential backoff (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
