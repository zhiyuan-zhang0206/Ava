"""Configuration slices of the IM Bridge daemon.

Each slice holds exactly the fields one part of the bridge reads, under the flat
field name the registry, `.env` and the config API already use. The slices are
plain frozen dataclasses: `services/entrypoints/im_bridge/daemon.py` (the composition root)
is the only module that reads `settings` and builds them, and everything else
receives the slice it needs through its constructor. See
`future/infra/security/dependency-injection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImBridgeConfig:
    """What the bridge core, its gateway client, push watchdog and notice bridge read."""

    im_disabled_adapters: tuple[str, ...]
    im_send_retry_delays: tuple[float, ...]
    im_push_retry_backoff_seconds: float
    im_push_retry_jitter_seconds: float
    im_sse_read_timeout_seconds: float
    im_bridge_timeline_window: int
    im_bridge_replay_messages: int
    im_bridge_notice_reply_window_seconds: int
    im_bridge_notice_open_limit: int
    # The gateway's own display default, shared with `GET /api/notices/live`.
    notices_open_default_limit: int


@dataclass(frozen=True)
class TelegramCredentialsConfig:
    """The Telegram adapter's bot credentials and poll timing."""

    telegram_bot_token: str
    telegram_owner_id: int
    telegram_poll_timeout_seconds: int
    telegram_reconnect_base_delay_seconds: float
    telegram_reconnect_max_delay_seconds: float


@dataclass(frozen=True)
class FeishuCredentialsConfig:
    """The Feishu adapter's app credentials, REST timeout and poll fallback."""

    feishu_app_id: str
    feishu_app_secret: str
    feishu_rest_timeout_seconds: float
    feishu_poll_interval_seconds: float
    feishu_poll_chat_id: str
    # How old an unhandled message may be and still be replayed after a restart:
    # the agent side dead-letters older chat inbounds (`daemon` domain field).
    delivery_watchdog_stale_claimed_threshold_seconds: float
