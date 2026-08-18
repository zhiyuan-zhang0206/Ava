"""Tests for shared.cluster_drift — prod-source git introspection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared.cluster_drift import (
    _prod_source_dir,
    prod_source_branch_drift,
    prod_source_head_sha,
    prod_source_pin_relation,
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
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert prod_source_head_sha() is None


def test_head_sha_returns_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sha = _init_prod_source(tmp_path / "source")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert prod_source_head_sha() == sha


def test_branch_drift_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert prod_source_branch_drift() is None


def test_branch_drift_on_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert prod_source_branch_drift() is None


def test_branch_drift_feature_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source", branch="ava-7/fix")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert prod_source_branch_drift() == "ava-7/fix"


def test_prod_source_dir_resolves_from_ava_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_prod_source_dir follows ~/.local/bin/ava → <source>/.venv/bin/ava, so it finds
    the real install source regardless of $AVA_HOME layout (the cloud's source is at
    /opt/ava/source while $AVA_HOME is ~/.ava_gateway)."""
    source = tmp_path / "opt" / "ava" / "source"
    ava_bin = source / ".venv" / "bin" / "ava"
    ava_bin.parent.mkdir(parents=True)
    ava_bin.write_text("#!/bin/sh\n")
    link = tmp_path / "home" / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True)
    link.symlink_to(ava_bin)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: tmp_path / "home"))  # pyright: ignore[reportUnknownArgumentType]
    assert _prod_source_dir() == source


def test_prod_source_dir_falls_back_to_ava_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ava symlink → fall back to $AVA_HOME/source."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: tmp_path / "nohome"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "avahome")
    assert _prod_source_dir() == tmp_path / "avahome" / "source"


def test_pin_relation_aligned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    sha_a = _init_prod_source(source)
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: source)
    assert prod_source_pin_relation(sha_a, sha_a) == "aligned"


def test_pin_relation_ahead(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Checkout moved past the pin (a stray `git pull`): pin=A is an ancestor of head=B."""
    source = tmp_path / "source"
    sha_a = _init_prod_source(source)
    sha_b = _commit(source, "b", "B")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: source)
    assert prod_source_pin_relation(sha_a, sha_b) == "ahead"


def test_pin_relation_behind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Missed a rollout: head=A is an ancestor of pin=B."""
    source = tmp_path / "source"
    sha_a = _init_prod_source(source)
    sha_b = _commit(source, "b", "B")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: source)
    assert prod_source_pin_relation(sha_b, sha_a) == "behind"


def test_pin_relation_diverged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """B (main) and C (side branch off A) share ancestor A but neither is the other's."""
    source = tmp_path / "source"
    sha_a = _init_prod_source(source)
    sha_b = _commit(source, "b", "B")
    _git(source, "checkout", "-b", "side", sha_a)
    sha_c = _commit(source, "c", "C")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: source)
    assert prod_source_pin_relation(sha_b, sha_c) == "diverged"


def test_pin_relation_unknown_pin_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin commit not present in this checkout (never fetched) → unknown."""
    source = tmp_path / "source"
    sha_a = _init_prod_source(source)
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: source)
    assert prod_source_pin_relation("0" * 40, sha_a) == "unknown"


def test_pin_relation_unknown_no_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No readable git repo → unknown (best-effort, never raises)."""
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "nope")
    assert prod_source_pin_relation("a" * 40, "b" * 40) == "unknown"
