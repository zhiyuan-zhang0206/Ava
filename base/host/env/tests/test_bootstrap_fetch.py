"""Tests for base/host/env/bootstrap.py: fetch config from the gateway (unauthenticated)."""

import os
from pathlib import Path

import httpx
import pytest

from base.host.env import bootstrap


# NOTE: base/host/env/bootstrap.py reads os.environ directly (it must run BEFORE
# Settings is built — Settings imports require these values to be present).
# So tests against it use monkeypatch.setitem(os.environ, ...) — equivalent to
# setenv, but bypasses the no_os_environ Rule 2 ban on monkeypatch.setenv
# of Settings-managed aliases (which is the right ban for code that reads
# settings.X, the wrong one for code that reads os.environ).
def test_inject_config_updates_environ(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://cp")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://injected/x"},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.delitem(os.environ, "AVA_DB_URL", raising=False)
    result = bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://injected/x"
    assert result.db_authority_refusal is not None
    assert "no database authority" in result.db_authority_refusal


def test_inject_derives_missing_gateway_health_url(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """A pure runner probes the remote gateway, never localhost, when its first
    start carried no explicit health override."""
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.tailnet:8123/")
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {},  # pyright: ignore[reportUnknownArgumentType]
    )

    bootstrap.inject_config_from_gateway()

    assert os.environ["AVA_GATEWAY_HEALTH_URL"] == ("http://gateway.tailnet:8123/api/health")


def test_inject_preserves_explicit_gateway_health_url(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.tailnet:8123")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_HEALTH_URL", "http://health-proxy.tailnet/ready")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {},  # pyright: ignore[reportUnknownArgumentType]
    )

    bootstrap.inject_config_from_gateway()

    assert os.environ["AVA_GATEWAY_HEALTH_URL"] == "http://health-proxy.tailnet/ready"


def test_inject_overwrites_existing_env(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
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


def test_inject_without_gateway_url_fails_fast(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    # A pure runner with no AVA_GATEWAY_URL (never enrolled) must not start with
    # no config — the error names the remedy.
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_URL", raising=False)
    monkeypatch.delitem(os.environ, "AVA_GATEWAY_PORT", raising=False)
    called: list[object] = []
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: called.append("fetch"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(bootstrap.BootstrapFetchError, match=r"ava init .*--gateway-url"):
        bootstrap.inject_config_from_gateway()
    assert called == []  # never fetched without a URL


def test_inject_wraps_fetch_failure(monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path) -> None:
    # The raw httpx error is wrapped with the operator's remedy; the process
    # must not start with no config.
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")

    def _boom(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(bootstrap.BootstrapFetchError, match="gw:8000"):
        bootstrap.inject_config_from_gateway()


def test_inject_treats_blank_as_absent(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
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


def test_fetch_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
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
    assert retry_waits == [0.5]


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


def test_fetch_preserves_linear_connect_backoff(
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    calls = 0

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, str]:
            return {"AVA_DB_URL": "postgresql://ok/x"}

    def flaky_get(*_args: object, **_kwargs: object) -> _Resp:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("unreachable")
        return _Resp()

    monkeypatch.setattr(bootstrap, "dial_get", flaky_get)
    assert bootstrap.fetch_bootstrap_config("http://cp", attempts=3) == {
        "AVA_DB_URL": "postgresql://ok/x"
    }
    assert calls == 3
    assert retry_waits == [0.5, 1.0]


def test_fetch_one_attempt_preserves_connect_exception(
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    error = httpx.ConnectTimeout("unreachable")
    calls = 0

    def fail(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(bootstrap, "dial_get", fail)
    with pytest.raises(httpx.ConnectTimeout) as caught:
        bootstrap.fetch_bootstrap_config("http://cp", attempts=1)
    assert caught.value is error
    assert calls == 1
    assert retry_waits == []


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


def test_config_source_reads_the_homes_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # env unset -> the home's `.env` decides; a legacy `machine_serve_gateway` file is not read.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)
    (home / "machine_serve_gateway").write_text("true")
    assert bootstrap.config_source_is_local() is False
    (home / ".env").write_text("AVA_MACHINE_SERVE_GATEWAY=true\n")
    assert bootstrap.config_source_is_local() is True
    (home / ".env").write_text("AVA_MACHINE_SERVE_GATEWAY=false\n")
    assert bootstrap.config_source_is_local() is False


def test_config_source_env_wins_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "false")
    (home / ".env").write_text("AVA_MACHINE_SERVE_GATEWAY=true\n")
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
    # Point AVA_HOME at a fresh dir: the suite's tmpfs home `.env` declares
    # AVA_MACHINE_SERVE_AGENT_RUNNER (conftest), which the settings-free
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


def test_should_fetch_reads_the_homes_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The serve_agent_runner flag resolves env > the home's `.env`, settings-free."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", raising=False)
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    (home / "machine_serve_agent_runner").write_text("true")
    assert bootstrap.should_fetch_from_gateway() is False  # the legacy file is not read
    (home / ".env").write_text("AVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    assert bootstrap.should_fetch_from_gateway() is True


# ── P0 #2100: the parent config snapshot ─────────────────────────────────────


def _write_snapshot(home: Path, *, base_url: str, age_s: float, values: dict[str, str]) -> Path:
    import json as _json
    import time as _time

    snap = home / "run" / "bootstrap-snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        _json.dumps(
            {
                "v": bootstrap._SNAPSHOT_VERSION,
                "base_url": base_url,
                "written_at": _time.time() - age_s,
                "values": values,
            }
        ),
        encoding="utf-8",
    )
    return snap


@pytest.fixture
def _snapshot_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pin AVA_HOME to a fresh tmp home so snapshot state never leaks across tests.

    Also delitem every key a fake fetch below injects into os.environ:
    `inject_config_from_gateway` applies fetched values straight to the env, so
    without the deletion a test would leave e.g. a bogus AVA_DB_URL behind for
    every later test — including exec children, which inherit env AVA_DB_URL
    verbatim on the config-fetch-skip path (AVA_PROCESS_PROFILE=agent).
    monkeypatch restores the pre-test value at teardown."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:8000")
    for _key in ("AVA_DB_URL", "DEEPSEEK_API_KEY", "AVA_GATEWAY_HEALTH_URL"):
        monkeypatch.delitem(os.environ, _key, raising=False)
    return home


def test_inject_skips_fetch_on_fresh_snapshot(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """P0 #2100: a fresh parent snapshot is authoritative — no HTTP fetch at all."""
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=1.0,
        values={"AVA_DB_URL": "postgresql://snapshot/x", "DEEPSEEK_API_KEY": "snap-key"},
    )

    def _no_fetch(*_a: object, **_k: object) -> dict[str, str]:
        raise AssertionError("a fresh snapshot must skip the fetch entirely")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _no_fetch)  # pyright: ignore[reportUnknownArgumentType]
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://snapshot/x"
    assert os.environ["DEEPSEEK_API_KEY"] == "snap-key"
    assert os.environ["AVA_GATEWAY_HEALTH_URL"] == "http://gw:8000/api/health"


def test_inject_fetches_and_refreshes_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """A stale snapshot fetches as before; the fetch wins AND refreshes the cache."""
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=10_000.0,
        values={"AVA_DB_URL": "postgresql://old/x"},
    )
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://fresh/x"},  # pyright: ignore[reportUnknownArgumentType]
    )
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://fresh/x"  # fetch is authoritative
    values, age = bootstrap._read_config_snapshot("http://gw:8000")  # type: ignore[misc]
    assert values == {"AVA_DB_URL": "postgresql://fresh/x"}
    assert age < 10.0  # refreshed for the next process


def test_inject_falls_back_to_stale_snapshot_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """Gateway unreachable + a (stale) snapshot → continue on last-known config.

    The outage scenario behind P0 #2100: no cluster edit can land while the
    gateway is down, so the snapshot is the safest config there is."""
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=10_000.0,
        values={"AVA_DB_URL": "postgresql://last-known/x"},
    )

    def _boom(*_a: object, **_k: object) -> dict[str, str]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    bootstrap.inject_config_from_gateway()  # must not raise
    assert os.environ["AVA_DB_URL"] == "postgresql://last-known/x"


def test_inject_still_raises_on_transport_failure_without_snapshot(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """No snapshot to fall back on: the honest loud failure is unchanged."""

    def _boom(*_a: object, **_k: object) -> dict[str, str]:
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(bootstrap.BootstrapFetchError, match="no snapshot"):
        bootstrap.inject_config_from_gateway()


def test_inject_does_not_fall_back_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """401 (wrong/rotated secret) must fail loud — the snapshot fallback is for
    transport failures only, never for a gateway that rejects this runner."""
    monkeypatch.delitem(os.environ, "AVA_DB_URL", raising=False)
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=10_000.0,
        values={"AVA_DB_URL": "postgresql://old/x"},
    )

    def _unauthorized(*_a: object, **_k: object) -> dict[str, str]:
        request = httpx.Request("GET", "http://gw:8000/api/bootstrap")
        raise httpx.HTTPStatusError(
            "401 Unauthorized", request=request, response=httpx.Response(401, request=request)
        )

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _unauthorized)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(bootstrap.BootstrapFetchError):
        bootstrap.inject_config_from_gateway()
    assert "AVA_DB_URL" not in os.environ  # nothing applied


def test_inject_ignores_snapshot_from_another_gateway(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """A re-enrolled runner must never reuse the previous gateway's config."""
    _write_snapshot(
        _snapshot_home,
        base_url="http://old-gateway:9000",
        age_s=1.0,
        values={"AVA_DB_URL": "postgresql://old-gw/x"},
    )
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://new-gw/x"},  # pyright: ignore[reportUnknownArgumentType]
    )
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://new-gw/x"


def test_inject_ignores_malformed_snapshot(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """Garbage on disk degrades to the fetch path, never kills the boot."""
    snap = _snapshot_home / "run" / "bootstrap-snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://fetched/x"},  # pyright: ignore[reportUnknownArgumentType]
    )
    bootstrap.inject_config_from_gateway()
    assert os.environ["AVA_DB_URL"] == "postgresql://fetched/x"


# ── resolve_bootstrap_values — the read-only boot resolution (task #4080) ────


def test_resolve_returns_values_without_mutating_the_process(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    """The watchdog's pre-attempt gate asks the same question `inject_` answers;
    it must get the values without applying them to os.environ."""
    values = {"AVA_DB_URL": "postgresql://fetched/x", "DEEPSEEK_API_KEY": "fetched-key"}
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: values,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert bootstrap.resolve_bootstrap_values() == values
    assert "AVA_DB_URL" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_resolve_uses_a_fresh_snapshot_without_fetching(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=1.0,
        values={"AVA_DB_URL": "postgresql://snapshot/x"},
    )

    def _no_fetch(*_a: object, **_k: object) -> dict[str, str]:
        raise AssertionError("a fresh snapshot must skip the fetch entirely")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _no_fetch)  # pyright: ignore[reportUnknownArgumentType]
    assert bootstrap.resolve_bootstrap_values() == {"AVA_DB_URL": "postgresql://snapshot/x"}


def test_resolve_falls_back_to_the_stale_snapshot_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    _write_snapshot(
        _snapshot_home,
        base_url="http://gw:8000",
        age_s=10_000.0,
        values={"AVA_DB_URL": "postgresql://last-known/x"},
    )

    def _boom(*_a: object, **_k: object) -> dict[str, str]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    assert bootstrap.resolve_bootstrap_values() == {"AVA_DB_URL": "postgresql://last-known/x"}


def test_resolve_raises_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch, _snapshot_home: Path
) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, str]:
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(bootstrap.BootstrapFetchError, match="no snapshot"):
        bootstrap.resolve_bootstrap_values()
