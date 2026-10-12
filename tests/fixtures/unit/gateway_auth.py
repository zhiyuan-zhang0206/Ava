"""Default credentials for in-process clients of the assembled Gateway app.

The cluster API always authenticates: every test process carries the suite's
cluster secret (`tests.fixtures.env_bootstrap`), and the Gateway's middleware
verifies a request's `Authorization: Bearer <secret>` exactly as it does in
production. This module gives every `TestClient` built over the Gateway app that
bearer by default, so a test calls routes without configuring authentication and
only the tests of authentication itself say anything about credentials.

Scope is the Gateway app and nothing else: `TestClient`s over local FastAPI
stand-ins, and clients other than `TestClient`, are untouched. The patch never
imports `gateway.app` (unrelated tests must not depend on every router); it
compares against the module only once something else has imported it.

The bearer is the cluster secret current when the client is built, so a test that
monkeypatches `settings.data_plane.cluster_secret` before building its client
stays authenticated, and one that patches it afterwards is deliberately rejected.
An explicit `Authorization` header (even an empty one) always wins.

Helpers for the tests that need another posture:

- `without_credentials(client)` drops the default bearer (an unauthenticated caller).
- `disable_default_gateway_credentials(monkeypatch)` restores the stock `TestClient`
  for a module whose subject is authentication itself: every client it builds is
  anonymous until a test gives it a header.
- `gateway_credentials()` is the header dict, for raw `httpx` / header assertions.
"""

import sys
from typing import Any

import pytest
from starlette.testclient import TestClient

from base.config import settings
from base.host.net.auth import bearer_header

__all__ = [
    "disable_default_gateway_credentials",
    "gateway_credentials",
    "install_default_gateway_credentials",
    "without_credentials",
]

_STOCK_INIT = TestClient.__init__


def gateway_credentials() -> dict[str, str]:
    """The `Authorization` header of the suite's cluster secret as it stands now."""
    return bearer_header(settings.data_plane.cluster_secret)


def without_credentials(client: TestClient) -> TestClient:
    """Make `client` an unauthenticated caller: drop its default bearer. Returns `client`."""
    client.headers.pop("Authorization", None)
    return client


def disable_default_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build `TestClient`s over the Gateway app with no credentials, as a stranger would."""
    monkeypatch.setattr(TestClient, "__init__", _STOCK_INIT)


def _is_gateway_app(app: object) -> bool:
    module = sys.modules.get("gateway.app")
    return module is not None and app is getattr(module, "app", None)


def install_default_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every `TestClient` over the Gateway app the cluster secret as its bearer."""
    original = TestClient.__init__

    def init(self: TestClient, app: Any, *args: Any, **kwargs: Any) -> None:
        if _is_gateway_app(app):
            headers = dict(kwargs.get("headers") or {})
            if not any(name.lower() == "authorization" for name in headers):
                headers.update(gateway_credentials())
            kwargs["headers"] = headers
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
