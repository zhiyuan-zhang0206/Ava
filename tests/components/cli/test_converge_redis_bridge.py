"""Redis private-network bridge convergence, self-heal, and observation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.dataplane import redis_bridge
from base.cluster.dataplane.redis_bridge import RedisBridgeEndpoint
from base.cluster.machine.boot import machine_from_boot
from base.cluster.ports import new_home_ports
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge import redis_bridge as bridge
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.redis_bridge import relay
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


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

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=_open,  # type: ignore[arg-type]
            sleep=sleeps.append,
        )

    assert opened == [("10.64.0.7", 6380), ("10.64.0.7", 6380)]
    assert broken.closed
    assert sleeps == [relay.INITIAL_REBIND_DELAY_S]
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

    with pytest.raises(_StopTestError):
        relay.serve_forever(
            ("10.64.0.7", 6380),
            ("127.0.0.1", 6380),
            open_listener=_open,  # type: ignore[arg-type]
            sleep=sleeps.append,
        )

    assert sleeps == [1.0, 2.0, 4.0]


def test_converge_retires_the_standalone_launchd_job_and_installed_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> None:
    home = tmp_path / "home"
    installed = home / "redis-bridge" / "relay.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("# stale relay\n")
    plist = tmp_path / "LaunchAgents" / "com.meiva.redis-bridge.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("stale plist")
    loaded = True
    calls: list[tuple[str, ...]] = []

    def launchctl(*args: str) -> object:
        nonlocal loaded
        calls.append(args)
        if args[0] == "bootout":
            loaded = False
        return type("Result", (), {"returncode": 0 if loaded else 1, "stderr": ""})()

    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    monkeypatch.setattr(bridge, "launchd_plist_path", lambda: plist)
    monkeypatch.setattr(bridge, "launchctl", launchctl)
    ctx = ConvergeCtx(
        repo=tmp_path / "repo",
        meiva_home=home,
        roles=None,
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=operator_database,
        producer=operator_pipeline,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )

    bridge.retire_launchd_redis_bridge(ctx)

    assert ("bootout", f"gui/{bridge.os.getuid()}/{bridge.LABEL}") in calls
    assert not plist.exists()
    assert not installed.exists()


def test_retire_preserves_artifacts_when_launchd_job_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    installed = home / "redis-bridge" / "relay.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("# live relay\n")
    plist = tmp_path / "LaunchAgents" / "com.meiva.redis-bridge.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("live plist")

    monkeypatch.setattr(bridge, "launchd_plist_path", lambda: plist)
    monkeypatch.setattr(bridge, "job_loaded", lambda: True)
    monkeypatch.setattr(bridge, "bootout_and_wait", lambda: False)

    with pytest.raises(RuntimeError, match="remained loaded"):
        bridge.retire_launchd_bridge(home)

    assert plist.exists()
    assert installed.exists()


def test_probe_uses_authenticated_redis_ping_through_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_runtime: CommandRuntime,
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

    def _endpoint(
        _home: Path, *, machine_host: Callable[[], str], remote_data_plane: Callable[[], bool]
    ) -> RedisBridgeEndpoint:
        assert machine_host() == command_runtime.machine.host()
        assert remote_data_plane() == command_runtime.config.view.data_plane.is_remote
        return RedisBridgeEndpoint("10.64.0.7", 16380)

    monkeypatch.setattr(bridge, "redis_bridge_endpoint", _endpoint)
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:16380/0")
    monkeypatch.setattr("redis.Redis.from_url", _from_url)

    status = bridge.probe_redis_bridge(
        tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert status.serving
    assert calls == [
        (
            "redis://meiva:secret@10.64.0.7:16380/0",
            {"socket_connect_timeout": 3.0, "socket_timeout": 3.0},
        )
    ]


def test_probe_fails_when_ping_through_the_bridge_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_runtime: CommandRuntime,
) -> None:
    class _DeadClient:
        def ping(self) -> None:
            raise ConnectionError("connection refused")

        def close(self) -> None:
            return

    def _endpoint(
        _home: Path, *, machine_host: Callable[[], str], remote_data_plane: Callable[[], bool]
    ) -> RedisBridgeEndpoint:
        assert machine_host() == command_runtime.machine.host()
        assert remote_data_plane() == command_runtime.config.view.data_plane.is_remote
        return RedisBridgeEndpoint("10.64.0.7", 16380)

    monkeypatch.setattr(bridge, "redis_bridge_endpoint", _endpoint)
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:16380/0")
    monkeypatch.setattr("redis.Redis.from_url", lambda *_a, **_kw: _DeadClient())  # pyright: ignore[reportUnknownArgumentType]

    status = bridge.probe_redis_bridge(
        tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert status.required
    assert not status.serving
    assert status.detail == "connection refused"


def test_probe_reports_unconfigured_when_registry_record_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_runtime: CommandRuntime,
) -> None:
    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    command_runtime.config.set_field("cluster_secret", "cluster-secret")
    command_runtime.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")

    def _missing_record(_home: Path) -> None:
        return None

    command_runtime.machine.set(host="10.64.0.7")
    monkeypatch.setattr(redis_bridge, "get_record", _missing_record)

    status = bridge.probe_redis_bridge(
        tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert status.required
    assert not status.serving
    assert status.endpoint == ""
    assert status.detail == f"bridge unconfigured: no registry record for {tmp_path}"


def test_retire_step_runs_on_gateway_hosts() -> None:
    step = next(
        candidate
        for candidate in converge_host.CONVERGE_STEPS
        if candidate.name == "retire the standalone Redis bridge LaunchAgent"
    )

    assert step.apply is bridge.retire_launchd_redis_bridge
    assert step.roles == frozenset({"gateway"})
    assert step.host_global


def test_unsupported_platform_does_not_read_the_relay_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden_host() -> str:
        pytest.fail("An unsupported platform must not read machine identity")

    config = ConfigBoot()
    machine = machine_from_boot(config)
    monkeypatch.setattr(machine, "host", forbidden_host)
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    status = bridge.probe_redis_bridge(tmp_path, machine=machine, config=config)
    assert not status.required
    assert not config.prepared


def test_remote_relay_does_not_read_machine_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_runtime: CommandRuntime,
) -> None:
    def forbidden_host() -> str:
        pytest.fail("A remote relay must not read machine identity")

    command_runtime.config.set_field("db_url", "postgresql://meiva@192.0.2.10:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")
    monkeypatch.setattr(command_runtime.machine, "host", forbidden_host)
    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    status = bridge.probe_redis_bridge(
        tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )
    assert not status.required


def test_probe_uses_each_owners_live_url_and_recorded_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_runtime: CommandRuntime,
) -> None:
    second_config = snapshot_process_config()
    second = CommandRuntime(
        config=second_config,
        machine=machine_from_boot(second_config),
        helper=refuse_helper,
        host=command_runtime.host,
        timers=ManualTimers(),
    )
    urls: list[str] = []
    closed: list[str] = []

    class Client:
        def __init__(self, url: str) -> None:
            self.url = url

        def ping(self) -> bool:
            return True

        def close(self) -> None:
            closed.append(self.url)

    def from_url(url: str, **_kwargs: object) -> Client:
        urls.append(url)
        return Client(url)

    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    monkeypatch.setattr("redis.Redis.from_url", from_url)
    for index, command in enumerate((command_runtime, second)):
        command.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
        command.config.set_field("cluster_secret", "bearer")
        command.config.set_field("machine_host", f"10.64.0.{index + 7}")
        command.config.set_field(
            "redis_url", f"redis://meiva:secret{index}@127.0.0.1:{17380 + index}/0"
        )
        command.machine.reset()
        home = tmp_path / str(index)
        home.mkdir()
        ports = new_home_ports()
        ports["redis"] = 17380 + index
        (home / "start-intent.json").write_text(
            json.dumps(
                {"record": {"ports": ports, "gateway_home": str(home), "created_at": "test"}}
            )
        )
        status = bridge.probe_redis_bridge(home, machine=command.machine, config=command.config)
        assert status.endpoint == f"10.64.0.{index + 7}:{17380 + index}"
        assert status.serving
    command_runtime.config.set_field("redis_url", "redis://meiva:changed@127.0.0.1:17380/1")
    assert bridge.probe_redis_bridge(
        tmp_path / "0", machine=command_runtime.machine, config=command_runtime.config
    ).serving
    assert urls == [
        "redis://meiva:secret0@10.64.0.7:17380/0",
        "redis://meiva:secret1@10.64.0.8:17381/0",
        "redis://meiva:changed@10.64.0.7:17380/1",
    ]
    assert closed == urls
