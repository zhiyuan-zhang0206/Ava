"""`ava cluster release *` argparse surface.

Placed under `tests/lifecycle/release_operator/` rather than `tests/cli/`:
that directory is already at its frozen 20-entry budget
(`scripts/structure/baseline.json`), so a new CLI-surface test file for this
slice lives with the rest of this slice's tests instead.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

import pytest

from cli.parsers import build_parser
from cli.parsers import cluster as cluster_parsers


def _release_choices() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    cmd = next(a for a in parser._actions if a.dest == "cmd")
    cluster_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["cluster"]
    cluster_cmd = next(a for a in cluster_p._actions if a.dest == "cluster_cmd")
    release_p = cast("dict[str, argparse.ArgumentParser]", cluster_cmd.choices)["release"]
    release_cmd = next(a for a in release_p._actions if a.dest == "release_cmd")
    return cast("dict[str, argparse.ArgumentParser]", release_cmd.choices)


def test_release_group_has_all_five_verbs() -> None:
    assert set(_release_choices()) == {"prepare", "request", "adopt", "exclude", "status"}


def test_every_release_leaf_binds_a_handler_from_cli_parsers_cluster() -> None:
    """Mirrors `tests/cli/test_main_dispatch.py`'s repo-wide invariant for just
    this slice's leaves, since that file lives in a directory already at its
    frozen entry budget."""
    for leaf in _release_choices().values():
        func = leaf.get_default("func")
        assert callable(func)
        assert func.__module__ == "cli.parsers.cluster"


def test_prepare_requires_commit_and_inputs() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cluster", "release", "prepare"])
    with pytest.raises(SystemExit):
        parser.parse_args(["cluster", "release", "prepare", "--commit", "a" * 40])
    args = parser.parse_args(
        ["cluster", "release", "prepare", "--commit", "a" * 40, "--inputs", "/x/inputs.json"]
    )
    assert args.commit == "a" * 40
    assert args.inputs == "/x/inputs.json"
    assert args.repo is None


def test_prepare_accepts_an_explicit_repo(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "cluster",
            "release",
            "prepare",
            "--commit",
            "a" * 40,
            "--inputs",
            "/x/inputs.json",
            "--repo",
            str(tmp_path),
        ]
    )
    assert args.repo == str(tmp_path)


def test_request_requires_commit_and_out_and_defaults_exclude_empty() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cluster", "release", "request", "--commit", "a" * 40])
    args = parser.parse_args(
        ["cluster", "release", "request", "--commit", "a" * 40, "--out", "/x/out.json"]
    )
    assert args.exclude == []
    assert args.reason is None
    assert (args.receipt, args.watch_s) == (None, None)


def test_request_accepts_an_explicit_receipt_and_watch_window() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "cluster",
            "release",
            "request",
            "--commit",
            "a" * 40,
            "--out",
            "/x/out.json",
            "--receipt",
            "/x/receipt.json",
            "--watch-s",
            "60",
        ]
    )
    assert (args.receipt, args.watch_s) == ("/x/receipt.json", 60)


def test_exclude_requires_operation_unit_and_reason() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cluster", "release", "exclude", "--operation", "x", "--unit", "m:/h"])
    args = parser.parse_args(
        ["cluster", "release", "exclude", "--operation", "x", "--unit", "m:/h", "--reason", "gone"]
    )
    assert (args.operation, args.unit, args.reason) == ("x", "m:/h", "gone")


def test_request_accepts_repeated_exclude() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "cluster",
            "release",
            "request",
            "--commit",
            "a" * 40,
            "--out",
            "/x/out.json",
            "--exclude",
            "m1:h1",
            "--exclude",
            "m2:h2",
            "--reason",
            "paused",
        ]
    )
    assert args.exclude == ["m1:h1", "m2:h2"]
    assert args.reason == "paused"


def test_adopt_requires_receipt() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cluster", "release", "adopt"])
    args = parser.parse_args(["cluster", "release", "adopt", "--receipt", "/x/receipt.json"])
    assert args.receipt == "/x/receipt.json"


def test_status_defaults_and_json_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["cluster", "release", "status"])
    assert args.operation is None
    assert args.json is False
    args = parser.parse_args(["cluster", "release", "status", "--operation", "abc", "--json"])
    assert args.operation == "abc"
    assert args.json is True


def test_handlers_are_defined_in_cluster_parsers_module() -> None:
    for name in (
        "_h_cluster_release_prepare",
        "_h_cluster_release_request",
        "_h_cluster_release_adopt",
        "_h_cluster_release_exclude",
        "_h_cluster_release_status",
    ):
        assert hasattr(cluster_parsers, name)
