"""Immutable configuration shared by one gateway application's HTTP consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from base.config import ConfigBoot
from gateway.http.middleware.cors import cors_allowed_origins


@dataclass(frozen=True)
class GatewayConfig:
    """Restart-bound HTTP policy derived from the gateway process's config owner."""

    cluster_secret: str = field(repr=False)
    session_ttl_seconds: int
    mcp_endpoint_enabled: bool
    allowed_origins: tuple[str, ...]
    browser_origin: str
    session_cookie_secure: bool


def gateway_config_from_boot(config: ConfigBoot) -> GatewayConfig:
    """Snapshot HTTP policy without creating or booting another config owner."""
    gateway = config.view.gateway
    services = config.view.services
    gateway_url: str = gateway.gateway_url
    explicit_secure: bool | None = gateway.session_cookie_secure
    return GatewayConfig(
        cluster_secret=config.view.data_plane.cluster_secret,
        session_ttl_seconds=gateway.session_ttl_seconds,
        mcp_endpoint_enabled=gateway.mcp_endpoint_enabled,
        allowed_origins=tuple(
            cors_allowed_origins(
                explicit=gateway.cors_allowed_origins,
                frontend_healthcheck_url=services.frontend_healthcheck_url,
                app_port=services.app_port,
                browser_origin=gateway.browser_origin,
                gateway_url=gateway_url,
            )
        ),
        browser_origin=gateway.browser_origin,
        session_cookie_secure=(
            explicit_secure
            if explicit_secure is not None
            else urlsplit(gateway_url).scheme == "https"
        ),
    )
