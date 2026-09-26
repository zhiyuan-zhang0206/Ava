"""Root diagnostics observe native storage ownership without repairing permissions."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import redis

from cli.commands import _cluster_instance as instance
from cli.commands._pooler_stop import OwnedPooler
from services.ava_root.health import ProbeRunner
from services.ava_root_glue import diagnostic_probes as probes
from shared import cluster
from shared.config import settings
from tests._containers import _free_port, redis_server
from tests.cli.test_pooler_stop import native_pooler as native_pooler

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX data plane")


@pytest.mark.parametrize("owned", [True, False])
async def test_native_redis_diagnostic_observes_custody_without_acl_or_config_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    with redis_server() as url:
        monkeypatch.setattr(settings.data_plane, "redis_url", url)
        monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — redis client stubs
            configuration = client.config_get("*")  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            acl = client.acl_list()  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            directory = Path(str(configuration["dir"])) if owned else tmp_path / "another-home"
            monkeypatch.setattr(instance, "_redis_data_dir", lambda: directory)
            result = await ProbeRunner().observe(probes.redis_acl, 5)
            assert result.verdict.value == ("alive" if owned else "unavailable"), result.detail
            assert client.ping(), "diagnostics must leave the native server running"  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            assert client.acl_list() == acl  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            assert client.config_get("*") == configuration  # pyright: ignore[reportUnknownMemberType] — redis command stubs


async def test_absent_native_redis_diagnostic_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    monkeypatch.setattr(instance, "_redis_data_dir", lambda: tmp_path)
    result = await ProbeRunner().observe(probes.redis_acl, 5)
    assert result.verdict.value == "down", result.detail
    assert "no native Redis listener" in result.detail


async def test_native_pooler_diagnostic_uses_shared_custody(
    native_pooler: tuple[OwnedPooler, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    custodian, _pooled, _direct = native_pooler
    ports = cast("cluster.ClusterPorts", cluster.LEGACY_AVA_PORTS.copy())
    ports["pgbouncer"] = custodian.port
    record = cluster.ClusterRecord(
        ports=ports, gateway_home=str(custodian.config.parent.parent), created_at="fixture"
    )
    monkeypatch.setattr(cluster, "get_record", Mock(return_value=record))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    # The fixture pooler trusts its userlist; the probe authenticates as the
    # admin-console operator entry with the home's recorded credential.
    monkeypatch.setattr(
        "shared.cluster.authority.read_pooler_admin",
        Mock(return_value=SimpleNamespace(password="fixture-admin")),  # noqa: S106 — trusted fixture pooler
    )
    before = custodian.config.read_bytes()
    result = await ProbeRunner().observe(probes.pgbouncer, 5)
    assert result.verdict.value == "alive", result.detail
    assert custodian.identity.live()
    assert custodian.config.read_bytes() == before
