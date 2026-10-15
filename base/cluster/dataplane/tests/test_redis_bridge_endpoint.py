from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.cluster.dataplane.redis_bridge import (
    RedisBridgeEndpoint,
    RedisBridgeUnconfiguredError,
    redis_bridge_endpoint,
)


def _home_with_redis_port(tmp_path: Path, port: int) -> Path:
    ports = {"redis": port}
    (tmp_path / "start-intent.json").write_text(
        json.dumps({"record": {"ports": ports, "gateway_home": str(tmp_path), "created_at": "t"}})
    )
    return tmp_path


def _forbidden() -> str:
    pytest.fail("this answer must not read the machine host")


def test_a_private_address_on_macos_gets_the_recorded_redis_port(tmp_path: Path) -> None:
    home = _home_with_redis_port(tmp_path, 16380)
    endpoint = redis_bridge_endpoint(
        home, machine_host=lambda: "10.64.0.7", remote_data_plane=lambda: False, platform="darwin"
    )
    assert endpoint == RedisBridgeEndpoint("10.64.0.7", 16380)


def test_other_platforms_and_remote_data_planes_need_no_relay(tmp_path: Path) -> None:
    assert (
        redis_bridge_endpoint(
            tmp_path, machine_host=_forbidden, remote_data_plane=lambda: False, platform="linux"
        )
        is None
    )
    assert (
        redis_bridge_endpoint(
            tmp_path, machine_host=_forbidden, remote_data_plane=lambda: True, platform="darwin"
        )
        is None
    )


def test_a_loopback_machine_needs_no_relay(tmp_path: Path) -> None:
    assert (
        redis_bridge_endpoint(
            tmp_path,
            machine_host=lambda: "localhost",
            remote_data_plane=lambda: False,
            platform="darwin",
        )
        is None
    )


def test_a_required_relay_without_a_cluster_record_is_unconfigured(tmp_path: Path) -> None:
    with pytest.raises(RedisBridgeUnconfiguredError, match="no registry record"):
        redis_bridge_endpoint(
            tmp_path,
            machine_host=lambda: "10.64.0.7",
            remote_data_plane=lambda: False,
            platform="darwin",
        )
