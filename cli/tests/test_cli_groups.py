"""Top-level CLI surface after the start-path convergence.

The bring-up verbs collapsed into a single `ava start` (which births the cluster
on first run); the standalone `infra`/`gateway`/`host` groups are gone, and
cluster-level verbs live under `ava cluster`.
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
    assert {"start", "stop", "status", "pty", "cluster"} <= _top_choices()


def test_cluster_group_has_destroy_and_no_down_or_listing() -> None:
    choices = _cluster_choices()
    assert {"status", "destroy"} <= choices
    # `down` stopped a cluster at another home; the host runs one, and `ava stop` stops it.
    assert "down" not in choices
    # No host-level list of clusters exists to print (each home describes only itself).
    assert "ls" not in choices
    # The whole-cluster bounce left with the retired updater; `ava restart` is per unit.
    assert "restart" not in choices
    # The stranded-lease clearer left with the deploy lease: a stranded pause is
    # read with `ava maintenance status` and ended with `resume --cancel` / `repair`.
    assert "recover" not in choices


def test_start_rejects_retired_identity_flags(tmp_path: Path) -> None:
    """Identity is the home path: `ava start` is a pure bring-up and takes no
    --cluster / --gateway-home (both die as unrecognized arguments)."""
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["start", "--cluster", "foo"])
    with pytest.raises(SystemExit):
        p.parse_args(["start", "--gateway-home", str(tmp_path / "h")])


def test_cluster_destroy_acts_on_this_home_and_takes_no_path(tmp_path: Path) -> None:
    p = _build_parser()
    assert p.parse_args(["cluster", "destroy"]).drop_db is False
    assert p.parse_args(["cluster", "destroy", "--drop-db"]).drop_db is True
    with pytest.raises(SystemExit):
        p.parse_args(["cluster", "destroy", "--path", str(tmp_path / ".ava-t")])
    with pytest.raises(SystemExit):
        p.parse_args(["cluster", "down", "--path", str(tmp_path / ".ava-t")])


def test_cluster_destroy_has_no_flag_that_skips_its_confirmation() -> None:
    p = _build_parser()
    for flag in ("--yes", "-y", "--force"):
        with pytest.raises(SystemExit):
            p.parse_args(["cluster", "destroy", flag])


def test_enroll_entry_is_removed() -> None:
    with pytest.raises(SystemExit):
        main(["enroll", "--gateway", "https://gw.example.com"])


def test_host_enroll_no_longer_routes() -> None:
    """`ava host enroll` is gone — it must not be silently accepted."""
    with pytest.raises(SystemExit):
        main(["host", "enroll", "--gateway", "https://gw.example.com"])


def test_help_builds_parser_config_free(tmp_path: Path) -> None:
    """`ava --help` must build the parser WITHOUT loading Settings — no
    `_add_*_parser` may eager-import a `cli.commands.*` module at build time
    (some of which load Settings), or a fresh host with no .env can't even
    read --help.

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
