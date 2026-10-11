"""Browser entry identity never changes runner routing or legacy login."""

import shlex

import pytest
from pydantic import ValidationError

from base.cluster.derive import fe_build_env
from base.config import settings
from base.config.domains.gateway import GatewaySettings
from gateway.http.auth.cors import cors_allowed_origins, session_cookie_secure


@pytest.mark.parametrize(
    "value",
    [
        "http://console.example",
        "https://user:pass@console.example",
        "https://console.example/api",
        "https://console.example?x=1",
        "https://console.example#x",
        "https://console.example:99999",
    ],
)
def test_browser_entry_rejects_non_origin(value: str) -> None:
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"browser_origin": value})


def test_browser_entry_normalizes_origin() -> None:
    assert GatewaySettings.model_validate(
        {"browser_origin": "https://Console.Example:443/"}
    ).browser_origin == ("https://console.example")


def test_build_and_cors_use_entry_without_changing_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "browser_origin", "https://console.example")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://192.0.2.2:8800")
    monkeypatch.setattr(settings.gateway, "gateway_port", 8800)
    monkeypatch.setattr(settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr("base.cluster.derive.is_windows", lambda: False)
    env = dict(item.split("=", 1) for item in shlex.split(fe_build_env()))
    assert env == {
        "NEXT_PUBLIC_GATEWAY_PORT": "8800",
        "NEXT_PUBLIC_BROWSER_ORIGIN": "https://console.example",
    }
    assert "https://console.example" in cors_allowed_origins()
    assert "http://192.0.2.2:8800" in cors_allowed_origins()
    assert settings.gateway.gateway_url == "http://192.0.2.2:8800"


def test_explicit_cors_remains_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "browser_origin", "https://console.example")
    monkeypatch.setattr(settings.services, "app_port", 18069)
    monkeypatch.setattr(settings.gateway, "cors_allowed_origins", ["https://other.example"])
    assert cors_allowed_origins() == ["https://other.example"]


@pytest.mark.parametrize(("app_port", "app_origins"), [(18069, True), (None, False)])
def test_derived_cors_allows_only_the_reserved_app_port_on_loopback(
    monkeypatch: pytest.MonkeyPatch, app_port: int | None, app_origins: bool
) -> None:
    """A browser loading Next.js from the reserved app port reaches its gateway.

    The port is the one start reserved (AVA_APP_PORT), only on the loopback
    hosts Next.js binds — never another host — and nothing is guessed without it.
    """
    monkeypatch.setattr(settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(settings.gateway, "browser_origin", "")
    monkeypatch.setattr(settings.services, "frontend_healthcheck_url", "http://localhost:18055")
    monkeypatch.setattr(settings.services, "app_port", app_port)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://192.0.2.2:18054")
    app = ["http://localhost:18069", "http://127.0.0.1:18069"] if app_origins else []
    assert cors_allowed_origins() == [
        "http://localhost:18055",
        "http://127.0.0.1:18055",
        *app,
        "http://192.0.2.2:18054",
        "http://192.0.2.2:18055",
    ]


def test_https_cookie_policy_preserves_direct_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "browser_origin", "https://console.example")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://192.0.2.2:8800")
    monkeypatch.setattr(settings.gateway, "session_cookie_secure", None)
    assert session_cookie_secure("https://console.example/api/auth/login")
    assert session_cookie_secure("https://console.example:443/api/auth/login")
    assert not session_cookie_secure("http://192.0.2.2:8800/api/auth/login")
    assert not session_cookie_secure("http://console.example/api/auth/login")
    assert not session_cookie_secure("https://other.example/api/auth/login")
    assert not session_cookie_secure("https://console.example:8443/api/auth/login")
