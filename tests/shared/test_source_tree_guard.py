"""Regression guards for the source-tree integrity guard (detector + repair)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared import source_tree_guard as stg


def _git(source: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_source(source: Path) -> Path:
    """Create a real git repo at `source` with one commit on `main`; returns
    the checkout path."""
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "tracked.txt").write_text("x")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "init")
    return source


def _commit(source: Path, content: str, msg: str) -> str:
    """Add a commit on the current branch from `content`, returning its sha."""
    (source / "tracked.txt").write_text(content)
    _git(source, "add", ".")
    _git(source, "commit", "-m", msg)
    return _git(source, "rev-parse", "HEAD")


# --- detector ---


def test_clean_checkout_reports_no_violations(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")

    assert stg.source_tree_violations(repo) == ()


def test_untracked_file_outside_whitelist_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "junk.txt").write_text("j")

    violations = stg.source_tree_violations(repo)

    assert any("junk.txt" in v for v in violations)


def test_untracked_dir_outside_whitelist_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "junkdir").mkdir()
    (repo / "junkdir" / "inner.txt").write_text("i")

    violations = stg.source_tree_violations(repo)

    assert any("junkdir" in v for v in violations)


def test_whitelisted_runtime_artifacts_are_not_violations(tmp_path: Path) -> None:
    """The built frontend bundle (frontend/) is the one legal untracked tree."""
    repo = _init_source(tmp_path / "source")
    (repo / "frontend" / ".next").mkdir(parents=True)
    (repo / "frontend" / ".next" / "build.txt").write_text("b")
    (repo / "frontend" / "tsconfig.tsbuildinfo").write_text("t")

    assert stg.source_tree_violations(repo) == ()


def test_tracked_modification_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "tracked.txt").write_text("changed")

    violations = stg.source_tree_violations(repo)

    assert any("tracked change" in v and "tracked.txt" in v for v in violations)


def test_head_moved_off_installed_commit_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean-looking checkout (git status empty) is still tampered when HEAD
    no longer matches the last fully installed commit."""
    repo = _init_source(tmp_path / "source")
    first = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "y", "c2")
    monkeypatch.setattr("shared.source_integrity.get", lambda: first)

    violations = stg.source_tree_violations(repo)

    assert any("installed commit" in v for v in violations)


def test_non_git_checkout_is_quiet(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()

    assert stg.source_tree_violations(repo) == ()


# --- repair ---


def test_repair_resets_head_to_installed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_source(tmp_path / "source")
    first = _git(repo, "rev-parse", "HEAD")
    second = _commit(repo, "y", "c2")
    monkeypatch.setattr("shared.source_integrity.get", lambda: first)

    repair = stg.repair_source_tree(repo)

    assert repair is not None
    assert repair.reset_from == second
    assert repair.reset_to == first
    assert repair.errors == ()
    assert (repo / "tracked.txt").read_text() == "x"
    assert _git(repo, "rev-parse", "HEAD") == first


def test_repair_resets_dirty_tracked_files_at_installed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outage's main shape: a tracked file edited in place and never
    committed leaves HEAD == installed — the reset must still revert it."""
    repo = _init_source(tmp_path / "source")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr("shared.source_integrity.get", lambda: head)
    (repo / "tracked.txt").write_text("TAMPERED")
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    repair = stg.repair_source_tree(repo)

    assert repair is not None
    assert repair.reset_from == head
    assert repair.reset_to == head
    assert repair.errors == ()
    assert (repo / "tracked.txt").read_text() == "x"
    assert len(emitted) == 1  # a repair that acted must leave an audit trail


def test_repair_cleans_untracked_and_keeps_whitelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_source(tmp_path / "source")
    monkeypatch.setattr("shared.source_integrity.get", lambda: _git(repo, "rev-parse", "HEAD"))
    (repo / "junk.txt").write_text("j")
    (repo / "junkdir").mkdir()
    (repo / "junkdir" / "inner.txt").write_text("i")
    (repo / "frontend" / ".next").mkdir(parents=True)
    (repo / "frontend" / ".next" / "build.txt").write_text("b")

    repair = stg.repair_source_tree(repo)

    assert repair is not None
    assert repair.reset_from is None
    assert repair.errors == ()
    assert not (repo / "junk.txt").exists()
    assert not (repo / "junkdir").exists()
    assert (repo / "frontend" / ".next" / "build.txt").exists()
    assert repair.cleaned == ("junk.txt", "junkdir/inner.txt")
    assert repair.kept_whitelisted == ("frontend/.next/build.txt",)


def test_repair_emits_telemetry_when_it_acts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_source(tmp_path / "source")
    monkeypatch.setattr("shared.source_integrity.get", lambda: _git(repo, "rev-parse", "HEAD"))
    (repo / "junk.txt").write_text("j")
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    stg.repair_source_tree(repo)

    assert emitted == [
        (
            ("telemetry", "source_tree_reset"),
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "reset_from": None,
                    "reset_to": None,
                    "cleaned": ["junk.txt"],
                    "kept_whitelisted": [],
                },
            },
        )
    ]


def test_repair_on_clean_tree_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_source(tmp_path / "source")
    monkeypatch.setattr("shared.source_integrity.get", lambda: _git(repo, "rev-parse", "HEAD"))
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    repair = stg.repair_source_tree(repo)

    assert repair is not None
    assert repair.reset_from is None
    assert repair.cleaned == ()
    assert repair.errors == ()
    assert emitted == []


def test_repair_non_git_checkout_is_none(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()

    assert stg.repair_source_tree(repo) is None
