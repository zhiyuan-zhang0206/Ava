"""Tests for base.deploy.git.cluster_drift — prod-source git introspection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from base.deploy.git import cluster_drift
from base.deploy.git.cluster_drift import (
    _prod_source_dir,
    checkout_head_sha,
    prod_source_branch_drift,
    prod_source_head_sha,
    running_from_prod_source,
)


def _git(source: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_prod_source(source: Path, *, branch: str = "main") -> str:
    """Create a real git repo at `source` with one commit on `main`; optionally
    leave it checked out on a different branch. Returns the HEAD sha."""
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "f").write_text("x")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "init")
    if branch != "main":
        _git(source, "checkout", "-b", branch)
    return _git(source, "rev-parse", "HEAD")


def _commit(source: Path, content: str, msg: str) -> str:
    """Add a commit on the current branch from `content`, returning its sha."""
    (source / "f").write_text(content)
    _git(source, "add", ".")
    _git(source, "commit", "-m", msg)
    return _git(source, "rev-parse", "HEAD")


def test_head_sha_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No source repo → None (nothing to read)."""
    monkeypatch.setattr(
        "base.deploy.git.cluster_drift._prod_source_dir", lambda: tmp_path / "source"
    )
    assert prod_source_head_sha() is None


def test_head_sha_returns_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sha = _init_prod_source(tmp_path / "source")
    monkeypatch.setattr(
        "base.deploy.git.cluster_drift._prod_source_dir", lambda: tmp_path / "source"
    )
    assert prod_source_head_sha() == sha


def test_branch_drift_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "base.deploy.git.cluster_drift._prod_source_dir", lambda: tmp_path / "source"
    )
    assert prod_source_branch_drift() is None


def test_branch_drift_on_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source")
    monkeypatch.setattr(
        "base.deploy.git.cluster_drift._prod_source_dir", lambda: tmp_path / "source"
    )
    assert prod_source_branch_drift() is None


def test_branch_drift_feature_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source", branch="ava-7/fix")
    monkeypatch.setattr(
        "base.deploy.git.cluster_drift._prod_source_dir", lambda: tmp_path / "source"
    )
    assert prod_source_branch_drift() == "ava-7/fix"


def test_prod_source_dir_resolves_from_the_home_cli_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without `$AVA_HOME/source`, the home's own CLI link (`$AVA_HOME/ava` →
    `<source>/.venv/bin/ava`) names the checkout — the gateway-only layout keeps
    its source at /opt/ava/source while $AVA_HOME is ~/.ava_gateway."""
    source = tmp_path / "opt" / "ava" / "source"
    ava_bin = source / ".venv" / "bin" / "ava"
    ava_bin.parent.mkdir(parents=True)
    ava_bin.write_text("#!/bin/sh\n")
    home = tmp_path / "avahome"
    home.mkdir()
    (home / "ava").symlink_to(ava_bin)
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    assert _prod_source_dir() == source


def test_prod_source_dir_ignores_the_host_launcher_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The host's bare `ava` belongs to no cluster: with neither `$AVA_HOME/source`
    nor `$AVA_HOME/ava`, the answer is the home's own (absent) source."""
    link = tmp_path / "home" / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "prod" / "source" / "scripts" / "ava-launcher.sh")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: tmp_path / "home"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / "avahome")
    assert _prod_source_dir() == tmp_path / "avahome" / "source"


def test_checkout_head_sha_reads_an_explicit_checkout(tmp_path: Path) -> None:
    sha = _init_prod_source(tmp_path / "wt")
    assert checkout_head_sha(tmp_path / "wt") == sha
    assert checkout_head_sha(tmp_path / "absent") is None


def test_running_from_prod_source_recognizes_the_loaded_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The module anchors on the checkout it was imported from, not on its package depth."""
    checkout = Path(__file__).resolve().parents[4]
    monkeypatch.setattr(cluster_drift, "_prod_source_dir", lambda: checkout)
    assert running_from_prod_source() is True
    monkeypatch.setattr(cluster_drift, "_prod_source_dir", lambda: tmp_path)
    assert running_from_prod_source() is False
