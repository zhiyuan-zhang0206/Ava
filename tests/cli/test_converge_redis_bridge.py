"""Redis private-network bridge convergence, self-heal, and observation."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cli.commands import _converge
from cli.commands import _converge_redis_bridge as bridge
from cli.commands._converge_spec import ConvergeCtx
from services.redis_bridge import relay


class _StopTestError(RuntimeError):
    pass


class _BrokenListener:
    def __init__(self) -> None:
        self.closed = False
        self.accept_calls = 0

    def accept(self) -> None:
        self.accept_calls += 1
        if self.accept_calls == 1:
            raise OSError("descriptor is closed")
        raise _StopTestError

    def close(self) -> None:
        self.closed = True


class _RecoveredListener:
    def accept(self) -> None:
        raise _StopTestError

    def close(self) -> None:
        return


def test_relay_rebuilds_listener_after_accept_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The #5740 failure: accept errors must replace the dead descriptor."""
    broken = _BrokenListener()
    listeners = iter([broken, _RecoveredListener()])
    opened: list[tuple[str, int]] = []
    sleeps: list[float] = []

    def _open(address: tuple[str, int]) -> object:
        opened.append(address)
        return next(listeners)

    monkeypatch.setattr(relay, "_sleep", sleeps.append)

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=_open,  # type: ignore[arg-type]
        )

    assert opened == [("10.64.0.7", 6380), ("10.64.0.7", 6380)]
    assert broken.closed
    assert sleeps == [relay._INITIAL_REBIND_DELAY_S]
    assert "descriptor is closed; rebuilding listener" in capsys.readouterr().out


def test_listener_bind_failures_back_off_until_interface_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def _open(_address: tuple[str, int]) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise OSError("address not available")
        raise _StopTestError

    monkeypatch.setattr(relay, "_sleep", sleeps.append)

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=_open,  # type: ignore[arg-type]
        )

    assert sleeps == [1.0, 2.0, 4.0]


def test_plist_runs_the_repo_owned_installed_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    content = plistlib.loads(
        bridge._plist_content(home, bridge.RedisBridgeConfig("10.64.0.7", 16380))
    )

    assert content["ProgramArguments"] == [
        "/usr/bin/python3",
        str(home / "redis-bridge" / "relay.py"),
        "--listen-host",
        "10.64.0.7",
        "--listen-port",
        "16380",
        "--backend-port",
        "16380",
    ]
    assert content["KeepAlive"] is True
    assert content["RunAtLoad"] is True


def test_install_source_repairs_mode_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    destination = tmp_path / "installed" / "relay.py"
    source.write_text("# source\n")
    destination.parent.mkdir()
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o600)

    assert bridge._install_source(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o755


def test_converge_installs_source_and_loads_changed_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "services" / "redis_bridge" / "relay.py"
    source.parent.mkdir(parents=True)
    source.write_text("# authoritative source\n")
    home = tmp_path / "home"
    plist = tmp_path / "LaunchAgents" / "com.ava.redis-bridge.plist"
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(bridge, "_plist_path", lambda: plist)
    monkeypatch.setattr("shared.os_cron.os_jobs_enabled", lambda: True)

    def _launchctl(*args: str) -> object:
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(bridge, "_launchctl", _launchctl)

    bridge._ensure_launchd(home, repo, bridge.RedisBridgeConfig("10.64.0.7", 16380))

    assert (home / "redis-bridge" / "relay.py").read_bytes() == source.read_bytes()
    assert (home / "redis-bridge" / "relay.py").stat().st_mode & 0o111
    assert plist.exists()
    assert calls[-1] == ("bootstrap", f"gui/{bridge.os.getuid()}", str(plist))


def test_unchanged_loaded_job_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "services" / "redis_bridge" / "relay.py"
    source.parent.mkdir(parents=True)
    source.write_text("# source\n")
    home = tmp_path / "home"
    installed = home / "redis-bridge" / "relay.py"
    bridge._install_source(source, installed)
    plist = tmp_path / "LaunchAgents" / "com.ava.redis-bridge.plist"
    plist.parent.mkdir(parents=True)
    config = bridge.RedisBridgeConfig("10.64.0.7", 16380)
    plist.write_bytes(bridge._plist_content(home, config))
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(bridge, "_plist_path", lambda: plist)
    monkeypatch.setattr("shared.os_cron.os_jobs_enabled", lambda: True)

    def _launchctl(*args: str) -> object:
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(bridge, "_launchctl", _launchctl)

    bridge._ensure_launchd(home, repo, config)

    assert calls == [("print", f"gui/{bridge.os.getuid()}/{bridge._LABEL}")]


def test_probe_uses_authenticated_redis_ping_through_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, float]]] = []

    class _Client:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            return

    def _from_url(url: str, **kwargs: float) -> _Client:
        calls.append((url, kwargs))
        return _Client()

    def _config(_home: Path) -> bridge.RedisBridgeConfig:
        return bridge.RedisBridgeConfig("10.64.0.7", 16380)

    monkeypatch.setattr(bridge, "_bridge_config", _config)
    monkeypatch.setattr(bridge, "_job_loaded", lambda: True)
    monkeypatch.setattr(
        bridge.settings.data_plane,
        "redis_url",
        "redis://ava:secret@127.0.0.1:16380/0",
    )
    monkeypatch.setattr("redis.Redis.from_url", _from_url)

    status = bridge.probe_redis_bridge(tmp_path)

    assert status.serving
    assert status.supervised
    assert calls == [
        (
            "redis://ava:secret@10.64.0.7:16380/0",
            {"socket_connect_timeout": 3.0, "socket_timeout": 3.0},
        )
    ]


def test_probe_does_not_accept_a_loaded_job_when_ping_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _DeadClient:
        def ping(self) -> None:
            raise ConnectionError("connection refused")

        def close(self) -> None:
            return

    def _config(_home: Path) -> bridge.RedisBridgeConfig:
        return bridge.RedisBridgeConfig("10.64.0.7", 16380)

    monkeypatch.setattr(bridge, "_bridge_config", _config)
    monkeypatch.setattr(bridge, "_job_loaded", lambda: True)
    monkeypatch.setattr(
        bridge.settings.data_plane,
        "redis_url",
        "redis://ava:secret@127.0.0.1:16380/0",
    )
    monkeypatch.setattr("redis.Redis.from_url", lambda *_a, **_kw: _DeadClient())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    status = bridge.probe_redis_bridge(tmp_path)

    assert status.required
    assert not status.serving
    assert status.supervised
    assert status.detail == "connection refused"


def test_bridge_step_is_gateway_prod_host_only() -> None:
    step = next(
        candidate
        for candidate in _converge.CONVERGE_STEPS
        if candidate.name == "Redis private-network bridge"
    )

    assert step.apply is bridge.ensure_redis_bridge
    assert step.roles == frozenset({"gateway"})
    assert step.host_global
    assert step.requires_unit_config


def test_ensure_bridge_uses_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = ConvergeCtx(repo=tmp_path / "repo", ava_home=tmp_path / "home", roles=None)
    config = bridge.RedisBridgeConfig("10.64.0.7", 16380)
    calls: list[tuple[Path, Path, bridge.RedisBridgeConfig]] = []

    def _config(_home: Path) -> bridge.RedisBridgeConfig:
        return config

    def _ensure(home: Path, repo: Path, value: bridge.RedisBridgeConfig) -> None:
        calls.append((home, repo, value))

    monkeypatch.setattr(bridge, "_bridge_config", _config)
    monkeypatch.setattr(bridge, "_ensure_launchd", _ensure)

    bridge.ensure_redis_bridge(ctx)

    assert calls == [(ctx.ava_home, ctx.repo, config)]
