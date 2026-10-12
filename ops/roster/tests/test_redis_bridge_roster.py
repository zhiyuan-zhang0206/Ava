"""The redis-bridge unit: rostered for gateways, gated out where no relay is needed."""

from __future__ import annotations

import json

import pytest

from base.cluster.dataplane.redis_bridge import RedisBridgeEndpoint
from base.cluster.machine import MachineIdentity
from base.paths import ava_home
from ops import roster
from ops import spec as ops_spec
from ops.roster import build_services
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _bridge() -> ops_spec.ServiceSpec:
    return next(s for s in build_services() if s.session == "redis-bridge")


def test_the_bridge_is_a_gateway_unit_run_by_the_system_interpreter() -> None:
    bridge = _bridge()
    assert bridge.capabilities == frozenset({"gateway"})
    assert bridge.cmd.startswith("/usr/bin/python3 services/redis_bridge/relay.py")
    assert bridge.identity_probe is not None


def test_a_macos_gateway_with_a_private_address_is_not_gated(
    monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
) -> None:
    home = ava_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "start-intent.json").write_text(
        json.dumps(
            {"record": {"ports": {"redis": 16380}, "gateway_home": str(home), "created_at": "t"}}
        )
    )
    monkeypatch.setattr("base.cluster.dataplane.redis_bridge.sys.platform", "darwin")
    machine_identity.set(host="10.64.0.7")
    assert ops_spec._gate_reason(_bridge(), machine=machine_identity) is None


def test_the_unit_command_carries_the_recorded_listen_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        roster,
        "redis_bridge_endpoint",
        lambda *_a, **_k: RedisBridgeEndpoint("10.64.0.7", 16380),
    )
    command, host, port = roster._redis_bridge_command()
    assert command == (
        "/usr/bin/python3 services/redis_bridge/relay.py --listen-host 10.64.0.7 "
        "--listen-port 16380 --backend-port 16380"
    )
    assert (host, port) == ("10.64.0.7", 16380)


def test_a_non_macos_host_is_gated_out(
    monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
) -> None:
    monkeypatch.setattr("base.cluster.dataplane.redis_bridge.sys.platform", "linux")
    reason = ops_spec._gate_reason(_bridge(), machine=machine_identity)
    assert reason == "no off-box Redis ingress on this host"
