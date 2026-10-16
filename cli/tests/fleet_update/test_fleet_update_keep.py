from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster, decision_argvs, stub_decision

_KEEP_PARTIAL: dict[str, Any] = {
    "changed_files": 3,
    "components": [
        {
            "name": "pty-sessions",
            "services": ["pty-sessions"],
            "keep": True,
            "reason": "no changed file reaches it",
            "surface": 31,
            "hit_count": 0,
            "hits": [],
        },
        {
            "name": "browser",
            "services": ["browser", "browser-mcp"],
            "keep": False,
            "reason": "1 changed file(s) reach it",
            "surface": 152,
            "hit_count": 1,
            "hits": ["services/desktop/browser/daemon.py"],
        },
    ],
    "keep_services": ["pty-sessions"],
}

_KEEP_ALL: dict[str, Any] = {
    "changed_files": 1,
    "components": [
        {
            "name": "pty-sessions",
            "services": ["pty-sessions"],
            "keep": True,
            "reason": "no changed file reaches it",
            "surface": 31,
            "hit_count": 0,
            "hits": [],
        },
        {
            "name": "browser",
            "services": ["browser", "browser-mcp"],
            "keep": True,
            "reason": "no changed file reaches it",
            "surface": 152,
            "hit_count": 0,
            "hits": [],
        },
    ],
    "keep_services": ["pty-sessions", "browser", "browser-mcp"],
}


def _stop_commands(cluster: Cluster) -> list[str]:
    return [command for _, command in cluster.commands if "stop -y" in command]


def test_the_kept_services_ride_every_stop_command(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    stub_decision(fleet_update.REPO, _KEEP_PARTIAL)
    assert run("down") == 0
    stops = _stop_commands(cluster)
    assert len(stops) == 3
    assert all("stop -y --timeout 600 --keep-service pty-sessions" in c for c in stops)
    assert all("--keep-service browser" not in c for c in stops)
    out = capsys.readouterr().out
    assert "keep decision: pty-sessions=keep (no changed file reaches it)" in out
    assert "keep decision: browser=restart (1 changed: services/desktop/browser/daemon.py)" in out


def test_every_kept_service_lands_on_the_stop_command(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    stub_decision(fleet_update.REPO, _KEEP_ALL)
    assert run("down") == 0
    flags = " --keep-service pty-sessions --keep-service browser --keep-service browser-mcp"
    assert all(flags in c for c in _stop_commands(cluster))


def test_the_decision_judges_the_unswitched_head_and_a_rerun_judges_nothing(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    stub = stub_decision(fleet_update.REPO, _KEEP_PARTIAL)
    assert run("down") == 0
    (first,) = decision_argvs(stub)
    assert f"--old {cluster.new}" not in first and f"--new {cluster.new}" in first
    assert run("down") == 0
    first, second = decision_argvs(stub)
    assert f"--old {cluster.new} --new {cluster.new}" in second


def test_a_missing_decision_script_stops_everything(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    (fleet_update.REPO / "scripts" / "host_ops" / "fleet_update_keep.py").unlink()
    assert run("down") == 0
    assert "WARNING: keep decision failed" in capsys.readouterr().out
    assert all("--keep-service" not in c for c in _stop_commands(cluster))


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("import sys; sys.stderr.write('boom: not a repo\\n'); sys.exit(1)", "boom: not a repo"),
        ("print('not json at all')", "not usable"),
        ("import json; print(json.dumps({'components': []}))", "not usable"),
    ],
)
def test_a_decision_that_cannot_judge_stops_everything(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    body: str,
    detail: str,
) -> None:
    cluster, run = env
    (fleet_update.REPO / "scripts" / "host_ops" / "fleet_update_keep.py").write_text(body + "\n")
    assert run("down") == 0
    out = capsys.readouterr().out
    assert "WARNING: keep decision" in out and detail in out
    assert all("--keep-service" not in c for c in _stop_commands(cluster))


def test_restart_all_skips_the_decision(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    stub = stub_decision(fleet_update.REPO, _KEEP_ALL)
    assert run("down", "--restart-all") == 0
    assert not Path(str(stub) + ".argv").exists()
    assert "keep decision: skipped (--restart-all): every service stops" in capsys.readouterr().out
    assert all("--keep-service" not in c for c in _stop_commands(cluster))


def test_the_dry_run_prints_the_decision_and_the_flagged_stops(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    stub_decision(fleet_update.REPO, _KEEP_PARTIAL)
    assert run("down", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "keep decision: pty-sessions=keep" in out
    assert "stop -y --timeout 600 --keep-service pty-sessions" in out
    assert [k for k, _ in cluster.effects] == ["pre-verify"]
