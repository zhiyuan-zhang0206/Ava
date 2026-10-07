"""Remote-managed data plane (Task #1752): start / stop / status degrade to
reachability probes and clear skips instead of managing a foreign service.

The local instance management surface (`ensure_cluster_storage`,
`stop_cluster_instance`, the `ava status` data-plane section) must never run
against a data plane whose URLs name another host — the URL is the switch, and
the management plane keys off `settings.data_plane.is_remote`.
"""

from __future__ import annotations

import pytest

from cli.commands.data_plane import bringup as dp
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.lifecycle import start as start_mod
from tests.factories.data_plane import remote_cluster_record
from tests.factories.data_plane import remote_urls as remote_urls


@pytest.fixture
def _fake_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import cluster

    def record(_home: object) -> cluster.ClusterRecord:
        return remote_cluster_record()

    monkeypatch.setattr(cluster, "get_record", record)


# ─── ava start: skip local bring-up, probe the URLs ──────────────────────────


def test_start_remote_skips_local_instance_and_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _fake_record: None
) -> None:
    calls: list[str] = []

    def _no_local_instance(*_args: object, **_kwargs: object) -> int:
        calls.append("ensure_cluster_storage")
        return 0

    monkeypatch.setattr(ci, "ensure_cluster_storage", _no_local_instance)
    monkeypatch.setattr(dp, "remote_pg_reachable", lambda: (True, "postgres (10.9.8.7:5432)"))
    monkeypatch.setattr(dp, "remote_redis_reachable", lambda: (True, "redis (10.9.8.7:6380)"))

    rc = start_mod._ensure_gateway_data_plane()

    assert rc == 0
    assert calls == [], "local instance bring-up must not run against a remote data plane"
    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "skipping local instance bring-up" in out


def test_start_remote_unreachable_fails_fast_with_dial_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _fake_record: None
) -> None:
    def _no_local_instance(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("local bring-up must not run against a remote data plane")

    monkeypatch.setattr(ci, "ensure_cluster_storage", _no_local_instance)
    monkeypatch.setattr(
        dp,
        "remote_pg_reachable",
        lambda: (False, "postgres (10.9.8.7:5432) connect failed: connection refused"),
    )

    rc = start_mod._ensure_gateway_data_plane()

    assert rc == 1
    err = capsys.readouterr().err
    assert "remote data plane unreachable" in err
    assert "10.9.8.7" in err
    assert "AVA_DB_URL" in err
