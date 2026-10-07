"""Remote data-plane stop, status and leftover-local-instance diagnostics."""

import pytest

from base.config import settings
from cli.commands.data_plane import bringup as dp
from cli.commands.data_plane import cluster_instance as ci
from tests.factories.data_plane import remote_cluster_record
from tests.factories.data_plane import remote_urls as remote_urls

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
    from base import cluster

    rec = remote_cluster_record()
    monkeypatch.setattr(cluster, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_pg_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]
    # The remote-managed home carries no local Redis admin password, so the
    # leftover Redis is detected by its listener, never by an authenticated PING.
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    monkeypatch.setattr(dp, "_local_listener", lambda port: port == 18012)  # pyright: ignore[reportUnknownArgumentType]

    rc = ci.stop_cluster_instance()

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "remote-managed" in combined
    assert "local postgres + redis from before the switch is still running" in combined
    assert "no longer managed" in combined
