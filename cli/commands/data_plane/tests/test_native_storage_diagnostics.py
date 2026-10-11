"""Root diagnostics use storage protocols without repairing permissions."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import redis

from base import cluster
from base.config import settings
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.tests.test_pooler_stop import native_pooler as native_pooler
from services.supervision.ava_root.health import ProbeRunner
from services.supervision.ava_root_glue import diagnostic_probes as probes
from tests._containers import _free_port, redis_server

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX data plane")


async def test_native_redis_diagnostic_uses_authenticated_ping_without_acl_or_config_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        redis_server() as url,
        redis.Redis.from_url(url, decode_responses=True) as client,  # pyright: ignore[reportUnknownMemberType] — redis client stubs
    ):
        client.execute_command(  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            "ACL", "SETUSER", "ava_probe_fixture", "on", ">fixture-password", "+ping"
        )
        runtime_url = url.replace("redis://", "redis://ava_probe_fixture:fixture-password@")
        monkeypatch.setattr(settings.data_plane, "redis_url", runtime_url)
        configuration = client.config_get("*")  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        acl = client.acl_list()  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        result = await ProbeRunner().observe(probes.redis_acl, 5)
        assert result.verdict.value == "alive", result.detail
        assert client.ping(), "diagnostics must leave the native server running"  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        assert client.acl_list() == acl  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        assert client.config_get("*") == configuration  # pyright: ignore[reportUnknownMemberType] — redis command stubs


async def test_absent_native_redis_diagnostic_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    result = await ProbeRunner().observe(probes.redis_acl, 5)
    assert result.verdict.value == "down", result.detail
    assert "PING failed" in result.detail


async def test_native_pooler_diagnostic_authenticates_without_config_writes(
    native_pooler: tuple[OwnedPooler, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    custodian, _pooled, _direct = native_pooler
    ports = cluster.new_home_ports()
    ports["pgbouncer"] = custodian.port
    record = cluster.ClusterRecord(
        ports=ports, gateway_home=str(custodian.config.parent.parent), created_at="fixture"
    )
    monkeypatch.setattr(cluster, "get_record", Mock(return_value=record))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    # The fixture pooler trusts its userlist; the probe authenticates as the
    # admin-console operator entry with the home's recorded credential.
    monkeypatch.setattr(
        "base.cluster.authority.read_pooler_admin",
        Mock(return_value=SimpleNamespace(password="fixture-admin")),  # noqa: S106 — trusted fixture pooler
    )
    before = custodian.config.read_bytes()
    result = await ProbeRunner().observe(probes.pgbouncer, 5)
    assert result.verdict.value == "alive", result.detail
    assert custodian.identity.live()
    assert custodian.config.read_bytes() == before
