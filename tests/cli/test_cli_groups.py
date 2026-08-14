"""Top-level CLI surface after the start-path convergence.

The bring-up verbs collapsed into a single `ava start` (which births the cluster
on first run); the standalone `infra`/`gateway`/`host` groups are gone, and
cluster registry management lives under `ava cluster`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from cli.main import _build_parser, main


def _top_choices() -> set[str]:
    p = _build_parser()
    actions = [a for a in p._actions if a.dest == "cmd"]
    assert actions, "no 'cmd' subparser action found"
    choices = actions[0].choices
    assert choices is not None
    return set(choices)


def _cluster_choices() -> set[str]:
    # argparse types `.choices` as `Iterable[str] | None`, but a subparsers action
    # holds a name -> parser dict at runtime; cast so the subscript type-checks.
    p = _build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    cluster_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["cluster"]
    sub = next(a for a in cluster_p._actions if a.dest == "cluster_cmd")
    return set(cast("dict[str, object]", sub.choices))


def test_removed_bringup_groups_are_gone() -> None:
    """infra / gateway / host collapsed into start/stop/cluster — no dead verbs."""
    assert _top_choices().isdisjoint({"infra", "gateway", "host"})


def test_core_verbs_exist() -> None:
    assert {"start", "stop", "status", "cluster"} <= _top_choices()


def test_cluster_group_has_ls_down_and_destroy() -> None:
    assert {"status", "restart", "update", "ls", "down", "destroy"} <= _cluster_choices()


def test_start_rejects_retired_identity_flags(tmp_path: Path) -> None:
    """Identity is the home path: `ava start` is a pure bring-up and takes no
    --cluster / --gateway-home (both die as unrecognized arguments)."""
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["start", "--cluster", "foo"])
    with pytest.raises(SystemExit):
        p.parse_args(["start", "--gateway-home", str(tmp_path / "h")])


def test_cluster_verbs_take_path(tmp_path: Path) -> None:
    p = _build_parser()
    args = p.parse_args(["cluster", "destroy", "--path", str(tmp_path / ".ava-t")])
    assert args.path == str(tmp_path / ".ava-t")
    args = p.parse_args(["cluster", "down", "--path", str(tmp_path / ".ava-t")])
    assert args.path == str(tmp_path / ".ava-t")


def test_cluster_destroy_requires_path_flag() -> None:
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["cluster", "destroy"])  # --path is required


def test_bare_enroll_still_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Top-level `ava enroll` routes to run_enroll (the agent-runner join path)."""
    captured: list[list[str]] = []

    def fake_run_enroll(args):
        captured.append(list(args))  # pyright: ignore[reportUnknownArgumentType]
        return 0

    monkeypatch.setattr("cli.enroll.run_enroll", fake_run_enroll)  # pyright: ignore[reportUnknownArgumentType]

    result = main(["enroll", "--gateway", "https://gw.example.com"])
    assert result == 0
    assert captured == [["--gateway", "https://gw.example.com"]]


def test_host_enroll_no_longer_routes() -> None:
    """`ava host enroll` is gone — it must not be silently accepted."""
    with pytest.raises(SystemExit):
        main(["host", "enroll", "--gateway", "https://gw.example.com"])


def test_help_builds_parser_config_free(tmp_path: Path) -> None:
    """`ava --help` must build the parser WITHOUT loading Settings — no
    `_add_*_parser` may eager-import cli.commands (whose package __init__ loads
    Settings), or a fresh host with no .env can't even read --help.

    Run in a subprocess with AVA_HOME at an empty dir + every AVA_* stripped, so a
    stray Settings load fails with a missing-required-field ValidationError."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)  # empty dir -> no .env
    proc = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"`ava --help` failed config-free:\n{proc.stderr}"
    assert proc.stdout.startswith("usage: ava")
