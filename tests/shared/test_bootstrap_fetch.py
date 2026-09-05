"""Tests for shared/bootstrap.py: fetch config from the gateway (unauthenticated)."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import bootstrap, config


def test_fetch_bootstrap_config_against_live_endpoint(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Suite runs multi-host on: the gateway requires the cluster secret, and the
    # fetch reads it from os.environ. Set both ends so the live fetch authenticates.
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "live-secret")
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "live-secret")

    # Route shared.bootstrap's dial_get (shared.http_dial.get) through the
    # in-process ASGI app.
    def fake_get(url, **kw):
        with TestClient(app) as c:
            return c.get(url.replace("http://cp", ""), **kw)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    monkeypatch.setattr(bootstrap, "dial_get", fake_get)  # pyright: ignore[reportUnknownArgumentType]
    values = bootstrap.fetch_bootstrap_config("http://cp")
    # Bootstrap serves the runner projection. A multi-host gateway also rewrites
    # its loopback host to the reachable address for remote runners.
    from shared import runtime_config
    from shared.cluster.derive import RUNNER_DB_PASSWORD_ENV, RUNNER_ROLE
    from shared.url_secret import url_with_userinfo

    expected = url_with_userinfo(
        str(config.settings.data_plane.db_url),
        RUNNER_ROLE,
        runtime_config.read_env_aliases()[RUNNER_DB_PASSWORD_ENV],
    )
    reachable = config._self_machine_host()
    if not config.is_loopback_host(reachable):
        expected = config.url_with_host(expected, reachable)
    actual_parts = urlsplit(values["AVA_DB_URL"])
    expected_parts = urlsplit(expected)
    # libpq dial hints such as hostaddr are implementation-specific query
    # parameters. The runner projection's connection identity must still match.
    assert (
        actual_parts.scheme,
        actual_parts.username,
        actual_parts.password,
        actual_parts.hostname,
        actual_parts.port,
        actual_parts.path.lstrip("/"),
    ) == (
        expected_parts.scheme,
        expected_parts.username,
        expected_parts.password,
        expected_parts.hostname,
        expected_parts.port,
        expected_parts.path.lstrip("/"),
    )


# NOTE: shared/bootstrap.py reads os.environ directly (it must run BEFORE
# Settings is built — Settings imports require these values to be present).
# So tests against it use monkeypatch.setitem(os.environ, ...) — equivalent to
# setenv, but bypasses the lint_no_os_environ Rule 2 ban on monkeypatch.setenv
# of Settings-managed aliases (which is the right ban for code that reads
# settings.X, the wrong one for code that reads os.environ).
def test_inject_config_updates_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://cp")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://injected/x"},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.delitem(os.environ, "AVA_DB_URL", raising=False)
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://injected/x"


def test_inject_derives_missing_gateway_health_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure runner probes the remote gateway, never localhost, when enroll
    carries no explicit health override."""
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.tailnet:8123/")
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {},  # pyright: ignore[reportUnknownArgumentType]
    )

    bootstrap.inject_config_from_gateway()

    assert os.environ["AVA_GATEWAY_HEALTH_URL"] == ("http://gateway.tailnet:8123/api/health")


def test_inject_preserves_explicit_gateway_health_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.tailnet:8123")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_HEALTH_URL", "http://health-proxy.tailnet/ready")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {},  # pyright: ignore[reportUnknownArgumentType]
    )

    bootstrap.inject_config_from_gateway()

    assert os.environ["AVA_GATEWAY_HEALTH_URL"] == "http://health-proxy.tailnet/ready"


def test_inject_overwrites_existing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fetched values are authoritative (2026-08-01): a stale value in env/.env —
    # a pre-cutover materialization `_enforce_cluster_env_authority` pushed in,
    # or a forwarded copy from a spawning process — is overridden by the
    # gateway's view. There is no cache to keep, so nothing "wins" over the
    # fetch.
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://cp")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://stale/cached")
    monkeypatch.setitem(os.environ, "DEEPSEEK_API_KEY", "stale-key")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType]
            "AVA_DB_URL": "postgresql://gateway/current",
            "DEEPSEEK_API_KEY": "fetched-key",
        },
    )
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://gateway/current"  # fetch wins
    assert os.environ["DEEPSEEK_API_KEY"] == "fetched-key"  # fetch wins


def test_inject_without_gateway_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pure runner with no AVA_GATEWAY_URL (never enrolled) must not start with
    # no config — the error names the remedy.
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_URL", raising=False)
    monkeypatch.delitem(os.environ, "AVA_PRIMARY_GATEWAY_URL", raising=False)
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_PORT", raising=False)
    called: list[object] = []
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: called.append("fetch"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(bootstrap.BootstrapFetchError, match="ava enroll"):
        bootstrap.inject_config_from_gateway()
    assert called == []  # never fetched without a URL


def test_inject_wraps_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The raw httpx error is wrapped with the operator's remedy; the process
    # must not start with no config.
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")

    def _boom(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(bootstrap.BootstrapFetchError, match="gw:8000"):
        bootstrap.inject_config_from_gateway()


def test_inject_treats_blank_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # defense in depth: the spawn path UNSETS these keys, but inject must also
    # overwrite a stale empty value from the fetch rather than keep it.
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://cp")
    monkeypatch.setitem(os.environ, "DEEPSEEK_API_KEY", "")  # a stale empty value
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"DEEPSEEK_API_KEY": "real-fetched-key"},  # pyright: ignore[reportUnknownArgumentType]
    )
    bootstrap.inject_config_from_gateway()
    assert os.environ["DEEPSEEK_API_KEY"] == "real-fetched-key"


def test_inject_uses_deprecated_gateway_url_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    # AVA_PRIMARY_GATEWAY_URL is honored pre-Settings (Settings' AliasChoices
    # hasn't run yet at fetch time).
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_URL", raising=False)
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_PORT", raising=False)
    monkeypatch.setitem(os.environ, "AVA_PRIMARY_GATEWAY_URL", "http://legacy-gw")
    captured: dict[str, str] = {}

    def fake_fetch(base_url, **_k):
        captured["url"] = base_url
        return {}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", fake_fetch)  # pyright: ignore[reportUnknownArgumentType]
    bootstrap.inject_config_from_gateway()
    assert captured["url"] == "http://legacy-gw"


def test_fetch_retries_transient_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.time, "sleep", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, str]:
            return {"AVA_DB_URL": "postgresql://ok/x"}

    def flaky_get(_url, **_k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("gateway mid-restart")
        return _Resp()

    monkeypatch.setattr(bootstrap, "dial_get", flaky_get)  # pyright: ignore[reportUnknownArgumentType]
    out = bootstrap.fetch_bootstrap_config("http://cp")
    assert out["AVA_DB_URL"] == "postgresql://ok/x"
    assert calls["n"] == 2  # retried once after the transient connect error


def test_fetch_does_not_retry_readtimeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # a slow response (ReadTimeout) must NOT be retried -- stacking another full
    # timeout would blow past the spawn launch-confirm window.
    calls = {"n": 0}

    def slow_get(*_a, **_k):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow gateway")

    monkeypatch.setattr(bootstrap, "dial_get", slow_get)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(httpx.ReadTimeout):
        bootstrap.fetch_bootstrap_config("http://cp")
    assert calls["n"] == 1  # not retried


# ── config source derivation (AVA_CONFIG_SOURCE deleted) ──


def test_config_source_is_local_when_serve_gateway_env_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")
    assert bootstrap.config_source_is_local() is True


def test_config_source_is_remote_for_pure_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "false")
    assert bootstrap.config_source_is_local() is False
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY")
    assert bootstrap.config_source_is_local() is False  # absent = pure runner


def test_config_source_reads_the_serve_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # env unset -> $AVA_HOME/machine_serve_gateway file decides.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)
    (home / "machine_serve_gateway").write_text("true")
    assert bootstrap.config_source_is_local() is True
    (home / "machine_serve_gateway").write_text("false")
    assert bootstrap.config_source_is_local() is False


def test_config_source_env_wins_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "false")
    (home / "machine_serve_gateway").write_text("true")
    assert bootstrap.config_source_is_local() is False


def test_config_source_fetch_skip_does_not_change_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lite opt-out is orthogonal: derivation still says pure runner.
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "false")
    monkeypatch.setitem(os.environ, bootstrap.CONFIG_FETCH_ENV, bootstrap.CONFIG_FETCH_SKIP)
    assert bootstrap.config_source_is_local() is False


def test_should_fetch_only_for_enrolled_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fetch fires only for a CONFIGURED pure agent-runner: serve_agent_runner
    on AND a gateway URL present. The full decision at Settings build is
    `not config_source_is_local() and should_fetch_from_gateway()`; a gateway
    unit short-circuits at the first half. Bare checkouts and unenrolled runners
    resolve locally."""
    # Point AVA_HOME at a fresh dir: the suite's tmpfs home carries a
    # machine_serve_agent_runner file (conftest), which the settings-free
    # flag resolution would otherwise read.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)

    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", "true")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    assert bootstrap.should_fetch_from_gateway() is True

    # gateway unit: the caller never reaches should_fetch (local source wins)
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")
    assert bootstrap.config_source_is_local() is True

    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY")
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_URL", raising=False)  # unenrolled runner
    assert bootstrap.should_fetch_from_gateway() is False

    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER")  # bare checkout
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    assert bootstrap.should_fetch_from_gateway() is False


def test_should_fetch_reads_the_serve_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The serve_agent_runner flag resolves env > $AVA_HOME/machine_serve_agent_runner
    file, settings-free."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", raising=False)
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    (home / "machine_serve_agent_runner").write_text("true")
    assert bootstrap.should_fetch_from_gateway() is True
