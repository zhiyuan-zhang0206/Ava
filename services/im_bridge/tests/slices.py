"""Slices for the IM Bridge tests: the daemon's own builders over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.im_bridge import daemon
from services.im_bridge.config import (
    FeishuCredentialsConfig,
    ImBridgeConfig,
    TelegramCredentialsConfig,
)
from services.im_bridge.gateway_client import GatewayClient


def im_bridge_config(**overrides: Any) -> ImBridgeConfig:
    return replace(daemon.im_bridge_config(), **overrides)


def telegram_config(**overrides: Any) -> TelegramCredentialsConfig:
    return replace(daemon.telegram_config(), **overrides)


def feishu_config(**overrides: Any) -> FeishuCredentialsConfig:
    return replace(daemon.feishu_config(), **overrides)


def gateway_client(config: ImBridgeConfig | None = None) -> GatewayClient:
    return GatewayClient(
        config or im_bridge_config(), gateway_url="http://localhost:8000", cluster_secret=""
    )
