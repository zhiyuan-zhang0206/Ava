"""Permissions helper cases: a foreign-label job bound to our socket."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperClient
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    retire_env,
    write_agent_plist,
)


def test_foreign_label_job_bound_to_our_socket_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    mine = lifecycle.label()
    foreign = write_agent_plist(agents, "com.ava.permissions-helper.stray", sock)
    other_cluster = write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-other-abcdef12",
        str(tmp_path / ".." / "x") + "/.ava-other/run/permissions-helper.18010.sock",
    )
    own = write_agent_plist(agents, mine, sock)
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle.refuse_foreign_label_jobs()
    assert foreign.exists()
    assert recorded == []
    # ...while other clusters' jobs and our own are untouched.
    assert other_cluster.exists()
    assert own.exists()


def test_foreign_label_refusal_holds_when_the_job_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    write_agent_plist(agents, "com.ava.permissions-helper.stray", sock)

    def bootout_job_not_loaded(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 78, b"", b"")

    monkeypatch.setattr(lifecycle, "run_bounded", bootout_job_not_loaded)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle.refuse_foreign_label_jobs()
    assert (agents / "com.ava.permissions-helper.stray.plist").exists()


def test_unrelated_cluster_plists_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents = retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    other = write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-demo-1234abcd",
        str(tmp_path / ".." / "z") + "/.ava-demo/run/permissions-helper.1234.sock",
        env_key="AVA_PERMISSIONS_HELPER_SOCKET",
    )
    monkeypatch.setattr(lifecycle, "helper_socket", lambda: Path(sock))

    assert lifecycle.foreign_label_plists() == []
    assert other.exists()


def test_long_mac_ipc_path_refuses_before_signing_or_native_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    long_socket = tmp_path / ("s" * 104)
    monkeypatch.setattr(lifecycle, "helper_socket", lambda: long_socket)
    monkeypatch.setattr(
        lifecycle, "ensure_signing_cert", lambda: pytest.fail("must refuse before signing")
    )
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="shorter cluster home"):
        lifecycle.converge(helper=refuse_helper)


def test_overlong_socket_is_typed_unavailability() -> None:
    with pytest.raises(client.PermissionsHelperError, match="not reachable"):
        PermissionsHelperClient("/" + "x" * 200).ping()


@pytest.mark.parametrize("capability", ["root_stop_intent_v1", "helper_shutdown_v1"])
def test_helper_ping_refuses_missing_stop_capability(capability: str) -> None:
    from services.desktop.permissions_helper import lifecycle

    reply = {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True}
    reply.pop(capability)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="upgrade its signed artifact"):
        lifecycle.helper_answers_ping(serve(ScriptedHelper(ping=reply)))
