"""Shared scoped-webhook authentication for gateway ingest routes."""

from __future__ import annotations

import hmac
from typing import NamedTuple

from starlette.requests import Request

from shared.cluster_auth import verify_bearer
from shared.config import settings


class WebhookAuthentication(NamedTuple):
    """Admission result plus the credential fact established by the server."""

    authorized: bool
    source_verified_by: str | None


def authenticate_webhook(request: Request, *, provider: str) -> WebhookAuthentication:
    """Verify the alert-scoped token, cluster bearer, or tokenless loopback."""
    token = settings.alerts.webhook_token
    token_value = token.get_secret_value() if token is not None else ""
    if token_value:
        presented = request.headers.get("X-Alerts-Token") or request.headers.get(
            "X-Ops-Alerts-Token"
        )
        if presented and hmac.compare_digest(presented, token_value):
            return WebhookAuthentication(
                authorized=True,
                source_verified_by=f"webhook:{provider}",
            )
        # Grafana 13 webhook contact points can only use notifier-native
        # Authorization without storing a custom header in plaintext, so the
        # scoped webhook token is also valid as a bearer credential here.
        if verify_bearer(request.headers.get("Authorization"), token_value):
            return WebhookAuthentication(
                authorized=True,
                source_verified_by=f"webhook:{provider}",
            )
    if verify_bearer(
        request.headers.get("Authorization"),
        settings.data_plane.cluster_secret,
    ):
        return WebhookAuthentication(
            authorized=True,
            source_verified_by="cluster_bearer",
        )
    if not token_value:
        host = request.client.host if request.client else ""
        if host in ("127.0.0.1", "::1"):
            return WebhookAuthentication(authorized=True, source_verified_by=None)
    return WebhookAuthentication(authorized=False, source_verified_by=None)
