from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from cli.parsers import build_parser


def _top_choices() -> set[str]:
    p = build_parser()
    actions = [a for a in p._actions if a.dest == "cmd"]
    assert actions, "no 'cmd' subparser action found"
    choices = actions[0].choices
    assert choices is not None
    return set(choices)


def _cluster_choices() -> set[str]:
    p = build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    cluster_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["cluster"]
    sub = next(a for a in cluster_p._actions if a.dest == "cluster_cmd")
    return set(cast("dict[str, object]", sub.choices))


def test_core_verbs_exist() -> None:
    assert {"start", "stop", "status", "pty", "cluster"} <= _top_choices()


def test_cluster_group_has_status_and_destroy() -> None:
    assert {"status", "destroy"} <= _cluster_choices()


def test_cluster_destroy_acts_on_this_home_and_takes_no_path(tmp_path: Path) -> None:
    p = build_parser()
    assert p.parse_args(["cluster", "destroy"]).drop_db is False
    assert p.parse_args(["cluster", "destroy", "--drop-db"]).drop_db is True
    with pytest.raises(SystemExit):
        p.parse_args(["cluster", "destroy", "--path", str(tmp_path / ".ava-t")])


def test_cluster_destroy_has_no_flag_that_skips_its_confirmation() -> None:
    p = build_parser()
    for flag in ("--yes", "-y", "--force"):
        with pytest.raises(SystemExit):
            p.parse_args(["cluster", "destroy", flag])


def test_help_builds_parser_config_free(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)
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
