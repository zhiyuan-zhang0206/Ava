"""Real gateway credentials for SDK creation scenarios in the direct-process stack.

The private gateway verifies a fresh fixture bearer through production middleware.
Runner/ops keep their existing open test posture: this stack has no root launcher
or machine-token ledger. No product admission rule is weakened for the fixture.
"""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def authenticated_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give test HTTP clients and exec SDK children one private verified bearer."""
    token = uuid4().hex
    monkeypatch.setenv("AVA_API_TOKEN", token)
    original_init = httpx.Client.__init__

    def init(client: httpx.Client, **kwargs: Any) -> None:
        headers = httpx.Headers(kwargs.get("headers"))
        headers.setdefault("Authorization", f"Bearer {token}")
        kwargs["headers"] = headers
        original_init(client, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)


def create_app() -> FastAPI:
    """Enable real credential admission only in this throwaway gateway process."""
    from base.cluster.auth import delivered_token
    from base.config import settings
    from gateway.app import app

    token = delivered_token()
    if not token:
        raise RuntimeError("authenticated gateway fixture requires its private API token")
    settings.data_plane.cluster_secret = token
    settings.gateway.auth_middleware_enabled = True
    return app
