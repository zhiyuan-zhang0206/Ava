from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.cluster.dataplane import redis_bridge
from base.cluster.dataplane.redis_bridge import RedisBridgeEndpoint
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from base.paths import ava_home
from services.supervision import roster
from services.supervision.roster import build_services
from services.supervision.roster import spec as ops_spec


def _bridge() -> ops_spec.ServiceSpec:
    return next(s for s in build_services() if s.session == "redis-bridge")


def test_the_bridge_is_a_gateway_unit_run_by_the_system_interpreter() -> None:
    bridge = _bridge()
    assert bridge.capabilities == frozenset({"gateway"})
    assert bridge.cmd.startswith("/usr/bin/python3 services/redis_bridge/relay.py")
    assert bridge.identity_probe is not None


def test_a_macos_gateway_with_a_private_address_is_not_gated(
    monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    home = ava_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "start-intent.json").write_text(
        json.dumps(
            {"record": {"ports": {"redis": 16380}, "gateway_home": str(home), "created_at": "t"}}
        )
    )
    monkeypatch.setattr(redis_bridge.sys, "platform", "darwin")
    machine_identity.set(host="10.64.0.7")
    assert ops_spec.gate_reason(_bridge(), machine=machine_identity, host=fake_host()) is None


def test_the_unit_command_carries_the_recorded_listen_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fixed_endpoint(*_args: object, **_kwargs: object) -> RedisBridgeEndpoint:
        return RedisBridgeEndpoint("10.64.0.7", 16380)

    monkeypatch.setattr(roster, "redis_bridge_endpoint", fixed_endpoint)
    command, host, port = roster.redis_bridge_command()
    assert command == (
        "/usr/bin/python3 services/redis_bridge/relay.py --listen-host 10.64.0.7 "
        "--listen-port 16380 --backend-port 16380"
    )
    assert (host, port) == ("10.64.0.7", 16380)


def test_a_non_macos_host_is_gated_out(
    monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
) -> None:
    monkeypatch.setattr(redis_bridge.sys, "platform", "linux")
    reason = ops_spec.gate_reason(_bridge(), machine=machine_identity, host=fake_host())
    assert reason == "no off-box Redis ingress on this host"
