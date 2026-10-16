from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster, git, stub_decision


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Cluster, Callable[..., int]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--initial-branch=main")
    (repo / "base" / "sessions" / "pty").mkdir(parents=True)
    (repo / "base" / "sessions" / "pty" / "daemon.py").write_text("SESSIONS = 1\n")
    (repo / "services" / "supervision" / "healthchecks").mkdir(parents=True)
    (repo / "services" / "supervision" / "healthchecks" / "protocol_probe.py").write_text(
        "PROBE = 1\n"
    )
    for message in ("old", "new"):
        (repo / ".python-version").write_text("3.12.12\n")
        (repo / "uv.lock").write_text(message)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", message)
    cluster = Cluster(git(repo, "rev-parse", "HEAD~1"), git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(fleet_update, "REPO", repo)
    monkeypatch.setattr(fleet_update, "ssh", cluster.ssh)
    monkeypatch.setattr(fleet_update, "time", cluster)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    stub_decision(repo, {"changed_files": 0, "components": [], "keep_services": []})
    hosts = [
        *shlex.split("--gateway gw --runner mac --runner lin --log-dir"),
        str(tmp_path / "logs"),
    ]

    def run(half: str, *extra: str) -> int:
        new = ["--new", cluster.new] if half == "down" else []
        cluster.run_started_at = len(cluster.effects)
        return fleet_update.main([half, *hosts, *new, *extra])

    return cluster, run
