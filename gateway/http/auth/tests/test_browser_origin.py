"""Browser entry identity never changes runner routing or legacy login."""

import shlex

import pytest
from pydantic import ValidationError

from base.cluster.derive import fe_build_env
from base.config import ConfigBoot
from base.config.domains.gateway import GatewaySettings
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.http.middleware.cors import cors_allowed_origins, session_cookie_secure


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


def test_build_and_cors_use_entry_without_changing_gateway() -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origin", "https://console.example")
    owner.set_field("gateway_url", "http://192.0.2.2:8800")
    owner.set_field("cors_allowed_origins", [])
    env = dict(
        item.split("=", 1)
        for item in shlex.split(
            fe_build_env(gateway_port=8800, browser_origin="https://console.example")
        )
    )
    assert env == {
        "NEXT_PUBLIC_GATEWAY_PORT": "8800",
        "NEXT_PUBLIC_BROWSER_ORIGIN": "https://console.example",
    }
    cfg = gateway_config_from_boot(owner)
    assert "https://console.example" in cfg.allowed_origins
    assert "http://192.0.2.2:8800" in cfg.allowed_origins
    assert owner.view.gateway.gateway_url == "http://192.0.2.2:8800"


def test_explicit_cors_remains_authoritative() -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origin", "https://console.example")
    owner.set_field("app_port", 18069)
    owner.set_field("cors_allowed_origins", ["https://other.example"])
    assert gateway_config_from_boot(owner).allowed_origins == ("https://other.example",)
    assert cors_allowed_origins(
        explicit=["https://other.example"],
        frontend_healthcheck_url="http://localhost:18055",
        app_port=18069,
        browser_origin="https://console.example",
        gateway_url="",
    ) == ["https://other.example"]


@pytest.mark.parametrize(("app_port", "app_origins"), [(18069, True), (None, False)])
def test_derived_cors_allows_only_the_reserved_app_port_on_loopback(
    app_port: int | None, app_origins: bool
) -> None:
    """A browser loading Next.js from the reserved app port reaches its gateway.

    The port is the one start reserved (AVA_APP_PORT), only on the loopback
    hosts Next.js binds — never another host — and nothing is guessed without it.
    """
    app = ["http://localhost:18069", "http://127.0.0.1:18069"] if app_origins else []
    assert cors_allowed_origins(
        explicit=(),
        frontend_healthcheck_url="http://localhost:18055",
        app_port=app_port,
        gateway_url="http://192.0.2.2:18054",
        browser_origin="",
    ) == [
        "http://localhost:18055",
        "http://127.0.0.1:18055",
        *app,
        "http://192.0.2.2:18054",
        "http://192.0.2.2:18055",
    ]


def test_https_cookie_policy_preserves_direct_http() -> None:
    owner = ConfigBoot()
    owner.set_field("browser_origin", "https://console.example")
    owner.set_field("gateway_url", "http://192.0.2.2:8800")
    owner.set_field("session_cookie_secure", None)
    cfg = gateway_config_from_boot(owner)
    assert not cfg.session_cookie_secure
    assert session_cookie_secure(
        "https://console.example/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )
    assert session_cookie_secure(
        "https://console.example:443/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )
    assert not session_cookie_secure(
        "http://192.0.2.2:8800/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )
    assert not session_cookie_secure(
        "http://console.example/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )
    assert not session_cookie_secure(
        "https://other.example/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )
    assert not session_cookie_secure(
        "https://console.example:8443/api/auth/login",
        browser_origin=cfg.browser_origin,
        default_secure=cfg.session_cookie_secure,
    )


def test_gateway_config_normalizes_origins_and_hides_cluster_secret_in_repr() -> None:
    owner = ConfigBoot()
    owner.set_field("cluster_secret", "top-secret-token")
    owner.set_field("cors_allowed_origins", ["https://one.example", "http://two.example:3000"])
    cfg = gateway_config_from_boot(owner)
    assert isinstance(cfg, GatewayConfig)
    assert cfg.allowed_origins == ("https://one.example", "http://two.example:3000")
    assert "top-secret-token" not in repr(cfg)
