"""Destroy closes native ownership before freeing a home's port reservation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from cli.commands import cluster_lifecycle as lifecycle
from shared import cluster
from shared.port_block import PORT_OFFSETS


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: registry)
    rec = cluster.ClusterRecord(
        {name: 20000 + offset for name, offset in PORT_OFFSETS.items()},
        str(home),
        "2026-09-25T00:00:00Z",
    )
    cluster.save_record(rec)
    monkeypatch.setattr(lifecycle, "cmd_cluster_down", lambda **_kw: 0)
    monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", lambda _home: None)
    monkeypatch.setattr(
        "services.permissions_helper.launchd_job.unregister_helper", lambda *_a, **_kw: None
    )
    return home


def test_destroy_checks_helper_before_freeing_reservation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.permissions_helper import launchd_job

    seen: list[Path] = []

    def unregister(target: Path, *, helper_port: int) -> None:
        assert cluster.get_record(home) is not None
        assert (home / "destroy-intent.json").exists()
        assert helper_port == cluster.get_record(home).ports["permissions_helper"]
        seen.append(target)

    monkeypatch.setattr(launchd_job, "unregister_helper", unregister)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert seen == [home]
    assert cluster.get_record(home) is None
    assert '"detached"' in (home / "destroy-intent.json").read_text()


def test_stop_failure_retains_reservation_and_never_unloads_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "cmd_cluster_down", lambda **_kw: 4)
    unload = Mock()
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", unload)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 4
    assert cluster.get_record(home) is not None
    unload.assert_not_called()


@pytest.mark.parametrize("stage", ["jobs", "helper"])
def test_ambiguous_cleanup_retains_reservation(
    home: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ownership unknown")

    if stage == "jobs":
        monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", fail)
    else:
        monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", fail)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert cluster.get_record(home) is not None


def test_destroy_preserves_credentials_and_data(home: Path) -> None:
    env = home / ".env"
    env.write_text("PRIVATE_KEY=retain\n")
    data = home / "pg" / "data"
    data.mkdir(parents=True)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert env.read_text() == "PRIVATE_KEY=retain\n"
    assert data.is_dir()


def test_destroy_refuses_default_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster, "is_default_home", lambda _home: True)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert cluster.get_record(home) is not None


def test_destroy_refuses_unknown_home(home: Path) -> None:
    other = home.parent / "unclaimed"
    assert lifecycle.cmd_cluster_destroy(path=str(other)) == 1
    assert not other.exists()


def test_down_uses_target_home_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Restore the real down implementation, not the destroy fixture's stop stub.
    import importlib

    importlib.reload(lifecycle)
    seen: list[dict[str, str]] = []

    def run(_argv: object, **kwargs: object):
        seen.append(kwargs["env"])
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    monkeypatch.setenv("AVA_DB_URL", "postgresql://foreign.invalid/foreign")
    assert lifecycle.cmd_cluster_down(path=str(home)) == 0
    assert seen[0]["AVA_HOME"] == str(home)
    assert "AVA_DB_URL" not in seen[0]
