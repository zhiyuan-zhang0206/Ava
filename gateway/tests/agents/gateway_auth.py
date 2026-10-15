from typing import Any

import pytest
from starlette.testclient import TestClient

from base.host.net.auth import bearer_header
from gateway.http.config import GatewayConfig

__all__ = [
    "disable_default_gateway_credentials",
    "gateway_credentials",
    "install_default_gateway_credentials",
    "without_credentials",
]

_STOCK_INIT = TestClient.__init__


def gateway_credentials(config: GatewayConfig) -> dict[str, str]:
    return bearer_header(config.cluster_secret)


def without_credentials(client: TestClient) -> TestClient:
    client.headers.pop("Authorization", None)
    return client


def disable_default_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TestClient, "__init__", _STOCK_INIT)


def _gateway_config(app: object) -> GatewayConfig | None:
    state = getattr(app, "state", None)
    config = getattr(state, "gateway_config", None)
    return config if isinstance(config, GatewayConfig) else None


def install_default_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    original = TestClient.__init__

    def init(self: TestClient, app: Any, *args: Any, **kwargs: Any) -> None:
        config = _gateway_config(app)
        if config is not None:
            headers = dict(kwargs.get("headers") or {})
            if not any(name.lower() == "authorization" for name in headers):
                headers.update(gateway_credentials(config))
            kwargs["headers"] = headers
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
