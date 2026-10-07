"""Permissions helper cases: old main job bound to our socket."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.desktop.permissions_helper import client
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    _retire_env,
    _write_agent_plist,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)


def test_old_main_job_bound_to_our_socket_is_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    mine = lifecycle._label()
    old_main = _write_agent_plist(agents, "com.ava.permissions-helper.main", sock)
    other_cluster = _write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-other-abcdef12",
        str(tmp_path / ".." / "x") + "/.ava-other/run/permissions-helper.18010.sock",
    )
    own = _write_agent_plist(agents, mine, sock)
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle._refuse_stale_jobs()
    assert old_main.exists()
    assert recorded == []
    # ...while other clusters' jobs and our own are untouched.
    assert other_cluster.exists()
    assert own.exists()


def test_retire_is_idempotent_when_job_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    _write_agent_plist(agents, "com.ava.permissions-helper.main", sock)
    monkeypatch.setattr(
        lifecycle,
        "run_bounded",
        lambda cmd, **_kw: __import__("subprocess").CompletedProcess(  # pyright: ignore[reportUnknownArgumentType]
            cmd, 78, b"", b""
        ),  # bootout: job not loaded
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle._refuse_stale_jobs()
    assert (agents / "com.ava.permissions-helper.main.plist").exists()


def test_unrelated_cluster_plists_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    other = _write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-demo-1234abcd",
        str(tmp_path / ".." / "z") + "/.ava-demo/run/permissions-helper.1234.sock",
        env_key="AVA_PERMISSIONS_HELPER_SOCKET",
    )
    monkeypatch.setattr(lifecycle, "permissions_helper_socket", lambda: Path(sock))

    assert lifecycle._stale_plists() == []
    assert other.exists()


def test_long_mac_ipc_path_refuses_before_signing_or_native_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    long_socket = tmp_path / ("s" * 104)
    monkeypatch.setattr(lifecycle, "permissions_helper_socket", lambda: long_socket)
    monkeypatch.setattr(
        lifecycle, "ensure_signing_cert", lambda: pytest.fail("must refuse before signing")
    )
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="shorter cluster home"):
        lifecycle.converge()


def test_overlong_socket_is_typed_unavailability() -> None:
    with pytest.raises(client.PermissionsHelperError, match="not reachable"):
        client.ping(sock_path="/" + "x" * 200)


@pytest.mark.parametrize("capability", ["root_stop_intent_v1", "helper_shutdown_v1"])
def test_helper_ping_refuses_missing_stop_capability(
    monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    from services.desktop.permissions_helper import client, lifecycle

    reply = {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True}
    reply.pop(capability)
    monkeypatch.setattr(client, "ping", lambda: reply)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="upgrade its signed artifact"):
        lifecycle._helper_answers_ping()
