"""Browser entry identity never changes runner routing or legacy login."""

import shlex

import pytest
from pydantic import ValidationError

from gateway._cors import cors_allowed_origins, session_cookie_secure
from shared.cluster.derive import fe_build_env
from shared.config import settings
from shared.config.gateway import GatewaySettings


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
    monkeypatch.setattr("shared.cluster.derive.IS_WINDOWS", False)
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
    monkeypatch.setattr(settings.gateway, "cors_allowed_origins", ["https://other.example"])
    assert cors_allowed_origins() == ["https://other.example"]


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
