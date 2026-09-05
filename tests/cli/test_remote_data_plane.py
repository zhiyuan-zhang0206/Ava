"""Remote-managed data plane (Task #1752): start / stop / status degrade to
reachability probes and clear skips instead of managing a foreign service.

The local instance management surface (`ensure_cluster_instance`,
`stop_cluster_instance`, the `ava status` data-plane section) must never run
against a data plane whose URLs name another host — the URL is the switch, and
the management plane keys off `settings.data_plane.is_remote`.
"""

from __future__ import annotations

import pytest

from cli.commands import _cluster_instance as ci
from cli.commands import _data_plane as dp
from cli.commands import start as start_mod
from shared.config import settings

_FOREIGN_DB = "postgresql://ava:pw@10.9.8.7:5432/ava"
_FOREIGN_REDIS = "rediss://ava:pw@10.9.8.7:6380/0"


@pytest.fixture(autouse=True)
def _remote_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the settings singleton at a foreign data plane for every test here
    (restored by monkeypatch after each test)."""
    monkeypatch.setattr(settings.data_plane, "db_url", _FOREIGN_DB)
    monkeypatch.setattr(settings.data_plane, "redis_url", _FOREIGN_REDIS)


@pytest.fixture
def _fake_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import cluster

    def _record(_home: object) -> cluster.ClusterRecord | None:
        from typing import cast

        return cluster.ClusterRecord(
            ports=cast(
                "cluster.ClusterPorts",
                {
                    "gateway": 18000,
                    "frontend": 18001,
                    "heartbeat": 18002,
                    "restarter": 18003,
                    "labeler": 18004,
                    "task_maintenance": 18005,
                    "memory_indexer": 18006,
                    "ops": 18007,
                    "milvus": 18008,
                    "browser": 18009,
                    "permissions_helper": 18010,
                    "postgres": 18011,
                    "redis": 18012,
                    "events_maintenance": 18014,
                    "delivery_watchdog": 18016,
                    "im_bridge": 18017,
                    "agent_host": 18019,
                    "pg_backup": 18021,
                },
            ),
            gateway_home=str(settings.general.ava_home),
            created_at="now",
        )

    monkeypatch.setattr(cluster, "get_record", _record)


# ─── ava start: skip local bring-up, probe the URLs ──────────────────────────


def test_start_remote_skips_local_instance_and_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _fake_record: None
) -> None:
    calls: list[str] = []

    def _no_local_instance(*_args: object, **_kwargs: object) -> int:
        calls.append("ensure_cluster_instance")
        return 0

    monkeypatch.setattr(ci, "ensure_cluster_instance", _no_local_instance)
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

    monkeypatch.setattr(ci, "ensure_cluster_instance", _no_local_instance)
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


# ─── ava stop: nothing to tear down locally ──────────────────────────────────


def test_stop_remote_is_a_noop_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _no_subprocess(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no local subprocess may run against a remote data plane")

    monkeypatch.setattr(ci.subprocess, "run", _no_subprocess)

    rc = ci.stop_cluster_instance()

    assert rc == 0
    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "nothing to stop locally" in out


# ─── ava status: probe the URLs, no local pooler line ────────────────────────


def test_status_remote_probes_urls_and_skips_pgbouncer_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dp, "remote_pg_reachable", lambda: (True, "postgres (10.9.8.7:5432)"))
    monkeypatch.setattr(dp, "remote_redis_reachable", lambda: (True, "redis (10.9.8.7:6380)"))
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)

    ci.print_data_plane_status()

    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "✓ postgres (10.9.8.7:5432)" in out
    assert "✓ redis (10.9.8.7:6380)" in out
    assert "pgbouncer" not in out, "a remote plane has no local pooler to display"


def test_status_remote_reports_unreachable_component(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dp, "remote_pg_reachable", lambda: (True, "postgres (10.9.8.7:5432)"))
    monkeypatch.setattr(
        dp,
        "remote_redis_reachable",
        lambda: (False, "redis (10.9.8.7:6380) connect failed: timeout"),
    )

    ci.print_data_plane_status()

    out = capsys.readouterr().out
    assert "✗ redis (10.9.8.7:6380) connect failed: timeout" in out


def test_stop_remote_warns_about_orphaned_local_instance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cluster that switched local→remote may still have its old local
    instance running; `ava stop` no longer manages it, so it must print a
    manual-teardown hint instead of silently leaving it (QA P2)."""
    from typing import cast

    from shared import cluster

    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {
                "gateway": 18000,
                "frontend": 18001,
                "heartbeat": 18002,
                "restarter": 18003,
                "labeler": 18004,
                "task_maintenance": 18005,
                "memory_indexer": 18006,
                "ops": 18007,
                "milvus": 18008,
                "browser": 18009,
                "permissions_helper": 18010,
                "postgres": 18011,
                "redis": 18012,
                "events_maintenance": 18014,
                "delivery_watchdog": 18016,
                "im_bridge": 18017,
                "agent_host": 18019,
                "pg_backup": 18021,
            },
        ),
        gateway_home=str(settings.general.ava_home),
        created_at="now",
    )
    monkeypatch.setattr(cluster, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_pg_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_redis_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]

    rc = ci.stop_cluster_instance()

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "remote-managed" in combined
    assert "still running" in combined
    assert "no longer managed" in combined
