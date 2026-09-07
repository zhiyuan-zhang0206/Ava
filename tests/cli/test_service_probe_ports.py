"""Tests that build_services() derives probe URLs/ports from settings.

Verifies that watchdog probe URLs follow health_port() and that milvus
tcp_port + frontend curl_url derive from settings rather than being
hardcoded literals.
"""

from __future__ import annotations

import pytest

import cli.commands._repo as repo
import ops.roster as spec_mod  # build_services + health_port live here; repo re-exports the roster
import shared.daemon_health as dh


def _spec_by_session(specs, session: str):
    return next(s for s in specs if s.session == session)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_daemon_probe_url_follows_health_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patching health_port() for 'labeler' must be reflected in build_services()."""

    def _fake_health_port(name: str) -> int:
        return 18003 if name == "labeler" else dh.DEFAULT_PORTS.get(name, 8000)

    # build_services reads health_port from ops.spec's namespace (repo just re-exports it).
    monkeypatch.setattr(spec_mod, "health_port", _fake_health_port)
    specs = repo.build_services()
    spec = _spec_by_session(specs, "labeler")
    assert spec.curl_url is not None
    assert "18003" in spec.curl_url


def test_milvus_tcp_port_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patching settings.services.milvus_port must be reflected in milvus spec's tcp_port."""
    monkeypatch.setattr(repo.settings.services, "milvus_port", 29530)
    spec = _spec_by_session(repo.build_services(), "milvus")
    assert spec.tcp_port == 29530


def test_frontend_curl_url_follows_app_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frontend healthcheck probes the Next.js app — AVA_APP_PORT when set,
    else the entry port + 1 (the entry itself is owned by the gate)."""
    monkeypatch.setattr(repo.settings.services, "frontend_healthcheck_url", "http://localhost:4000")
    # app_port() returns settings.services.app_port when set, else entry + 1 — a
    # polluted env (AVA_APP_PORT inherited from a sibling cluster's .env) pins
    # it and defeats the patch, so force the field back to None.
    monkeypatch.setattr(repo.settings.services, "app_port", None)
    spec = _spec_by_session(repo.build_services(), "frontend")
    assert spec.curl_url == "http://localhost:4001"


def test_frontend_curl_url_uses_explicit_app_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "frontend_healthcheck_url", "http://localhost:4000")
    monkeypatch.setattr(repo.settings.services, "app_port", 4800)
    spec = _spec_by_session(repo.build_services(), "frontend")
    assert spec.curl_url == "http://localhost:4800"


def test_frontend_cmd_port_follows_app_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frontend launch cmd passes -p <app port> (AVA_APP_PORT or entry+1)."""
    monkeypatch.setattr(repo.settings.services, "frontend_healthcheck_url", "http://localhost:4000")
    monkeypatch.setattr(repo.settings.services, "app_port", None)  # see test above
    spec = _spec_by_session(repo.build_services(), "frontend")
    assert "-p 4001" in spec.cmd


def test_frontend_cmd_port_uses_explicit_app_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "frontend_healthcheck_url", "http://localhost:4000")
    monkeypatch.setattr(repo.settings.services, "app_port", 4800)
    spec = _spec_by_session(repo.build_services(), "frontend")
    assert "-p 4800" in spec.cmd


def test_frontend_build_injects_gateway_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frontend build cmd must inline NEXT_PUBLIC_GATEWAY_PORT from settings.gateway.gateway_port.

    NEXT_PUBLIC_* are baked into the JS bundle at build time and the session
    only forwards AVA_* env, so the build subprocess never sees a .env-only
    NEXT_PUBLIC_GATEWAY_PORT — it must be injected on the command line from the
    single source of truth (AVA_GATEWAY_PORT). The browser then dials the gateway
    on that port (e.g. the prod VPS on 8800).
    """
    monkeypatch.setattr(repo.settings.gateway, "gateway_port", 8800)
    spec = _spec_by_session(repo.build_services(), "frontend")
    assert "NEXT_PUBLIC_GATEWAY_PORT=8800 npm run build" in spec.cmd


def test_gateway_curl_url_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway probe URL must follow settings.services.gateway_health_url."""
    monkeypatch.setattr(
        repo.settings.services, "gateway_health_url", "http://localhost:9999/api/agents"
    )
    spec = _spec_by_session(repo.build_services(), "gateway")
    assert spec.curl_url == "http://localhost:9999/api/agents"


def test_all_services_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_services() must return the expected complete service set."""
    monkeypatch.setattr(repo.settings.services, "browser_enabled", True)
    sessions = {s.session for s in repo.build_services()}
    expected = {
        "gateway",
        "im-bridge",
        "labeler",
        "heartbeat",
        "delivery-watchdog",
        "events-maintenance",
        "task-maintenance",
        "milvus",
        "memory-search",
        "memory-indexer",
        "frontend",
        "gateway-watchdog",
        "agent-runner-watchdog",
        "ops",
        "browser",
        "browser-mcp",
        "mcp-daemon",
        "computer-mcp",
        "page-server",
        "pg-backup",
        "pitr-uploader",
        "pitr-base-candidate",
        "otel-collector",
        "agent-host",
    }
    assert sessions == expected


def test_ops_probe_follows_health_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """ops curl_url must derive from health_port('ops')."""

    def _fake_health_port(name: str) -> int:
        return 18106 if name == "ops" else dh.DEFAULT_PORTS.get(name, 8000)

    monkeypatch.setattr(spec_mod, "health_port", _fake_health_port)
    spec = _spec_by_session(repo.build_services(), "ops")
    assert spec.curl_url is not None
    assert "18106" in spec.curl_url
