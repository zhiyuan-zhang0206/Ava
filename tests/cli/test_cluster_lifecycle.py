"""Destroy closes native ownership before marking a home detached."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from base import cluster
from cli.commands.cluster import home as lifecycle


def _detached(home: Path) -> bool:
    marker = home / "destroy-intent.json"
    return marker.exists() and '"detached"' in marker.read_text()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A born gateway home: its start intent records the cluster."""
    home = tmp_path / "home"
    home.mkdir()
    record = cluster.ClusterRecord(
        ports=cluster.new_home_ports(), gateway_home=str(home), created_at="test"
    )
    (home / cluster.INTENT_NAME).write_text(
        json.dumps({"record": asdict(record), "phase": "ready"})
    )

    def down(**_kw: object) -> int:
        return 0

    def unregister(_home: Path) -> None:
        return None

    def helper(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "cmd_cluster_down", down)
    monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", unregister)
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", helper)
    return home


def test_destroy_checks_helper_before_detaching(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.permissions_helper import launchd_job

    seen: list[Path] = []

    def unregister(target: Path, *, helper_port: int) -> None:
        rec = cluster.get_record(home)
        assert rec is not None
        assert (home / "destroy-intent.json").exists()
        assert helper_port == rec.ports["permissions_helper"]
        seen.append(target)

    monkeypatch.setattr(launchd_job, "unregister_helper", unregister)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert seen == [home]
    assert _detached(home)


def test_stop_failure_leaves_home_attached_and_never_unloads_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_stop(**_kw: object) -> int:
        return 4

    monkeypatch.setattr(lifecycle, "cmd_cluster_down", failed_stop)
    unload = Mock()
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", unload)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 4
    assert not _detached(home)
    unload.assert_not_called()


@pytest.mark.parametrize("stage", ["jobs", "helper"])
def test_ambiguous_cleanup_leaves_home_attached(
    home: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ownership unknown")

    if stage == "jobs":
        monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", fail)
    else:
        monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", fail)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not _detached(home)


def test_destroy_preserves_credentials_and_data(home: Path) -> None:
    env = home / ".env"
    env.write_text("PRIVATE_KEY=retain\n")
    data = home / "pg" / "data"
    data.mkdir(parents=True)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert env.read_text() == "PRIVATE_KEY=retain\n"
    assert data.is_dir()


def test_destroy_refuses_default_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def default(_home: Path) -> bool:
        return True

    monkeypatch.setattr(cluster, "is_default_home", default)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not (home / "destroy-intent.json").exists()


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
        seen.append(cast("dict[str, str]", kwargs["env"]))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    # cmd_cluster_down projects the child env straight from the live os.environ
    # (cli/commands/cluster/home.py), not the Settings singleton, so the raw-env
    # seam (not monkeypatch.setenv) is what the code under test actually strips.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://foreign.invalid/foreign")
    assert lifecycle.cmd_cluster_down(path=str(home)) == 0
    assert seen[0]["AVA_HOME"] == str(home)
    assert "AVA_DB_URL" not in seen[0]


def test_interrupted_cleanup_retries_to_completion(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup that could not finish leaves the home attached with its destroy
    intent retained; running destroy again completes it."""
    target = "services.permissions_helper.launchd_job.unregister_helper"

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("interrupted detach")

    def recovered(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(target, interrupted)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not _detached(home)
    assert (home / "destroy-intent.json").exists()

    monkeypatch.setattr(target, recovered)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert _detached(home)
