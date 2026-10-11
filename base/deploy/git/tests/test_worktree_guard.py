"""Tests for base/deploy/git/worktree_guard.py — the `git worktree remove` guard (issue #194)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from base.deploy.git.worktree_guard import find_live_anchors
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import new


def test_a_pty_session_anchored_under_the_target_is_reported(
    unit_home: Path, pty_service: PtyServiceProcess
) -> None:
    del pty_service
    target = unit_home / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    elsewhere = unit_home / "workspaces" / "9"
    elsewhere.mkdir(parents=True)
    new("ava-schedule-1", target)
    new("agent-shell", elsewhere)

    hits = find_live_anchors(target, run=unit_home / "run")

    # The session's live shell is also a process anchored there (hence a second hit).
    (reported,) = [hit for hit in hits if hit.startswith("pty session")]
    assert "ava-schedule-1" in reported and str(target) in reported
    assert not any("agent-shell" in hit for hit in hits)


def test_clean_when_nothing_anchored(unit_home: Path, pty_service: PtyServiceProcess) -> None:
    del pty_service
    target = unit_home / "worktrees" / "wt-under-test"
    elsewhere = unit_home / "workspaces" / "9"
    elsewhere.mkdir(parents=True)
    new("agent-shell", elsewhere)

    assert find_live_anchors(target, run=unit_home / "run") == []


def test_no_service_means_no_pty_session_and_the_home_is_not_created(tmp_path: Path) -> None:
    """The scan only reads: a home with no service neither holds a session nor gains a directory."""
    home = tmp_path / "never-created"

    assert find_live_anchors(tmp_path / "worktrees" / "wt", run=home / "run") == []
    assert not home.exists()


def test_live_process_cwd_anchor_reported(tmp_path: Path) -> None:
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    # start_new_session: a process genuinely anchored in the worktree is an
    # independent session, not a member of the checking invocation's job tree
    # (whose same-group processes the scan deliberately excludes, #3685).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=target, start_new_session=True
    )
    try:
        time.sleep(0.5)  # let the child start so psutil sees its cwd
        hits = find_live_anchors(target, run=tmp_path / "nope")
        assert any("process" in h and str(target) in h for h in hits)
    finally:
        child.kill()
        child.wait()


def _case_alias(path: Path) -> Path | None:
    """A differently-cased spelling of `path` that resolves to it.

    Exists only where the filesystem folds case (macOS APFS, WSL DrvFs);
    None on a case-sensitive filesystem. Flipping one letter at a time finds
    a foldable position even when the path crosses a case-sensitive mount
    boundary (e.g. /mnt/c on WSL).
    """
    text = str(path)
    for i, ch in enumerate(text):
        if not ch.isalpha():
            continue
        candidate = Path(text[:i] + ch.swapcase() + text[i + 1 :])
        try:
            if candidate.samefile(path):
                return candidate
        except OSError:
            continue
    return None


def test_case_alias_spelling_finds_anchors(
    tmp_path: Path, unit_home: Path, pty_service: PtyServiceProcess
) -> None:
    """#3707 QA (fail-open): on a case-insensitive filesystem a differently
    spelled target — `.../ava/...` from bash's logical pwd — names the same
    directory as the physical spelling psutil / the records report
    (`.../Ava/...`). The containment check must compare identity, not resolved
    strings: with the old string compare both arms below missed the anchor."""
    del pty_service
    target = unit_home / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    alias = _case_alias(target)
    if alias is None:
        pytest.skip("requires a case-insensitive filesystem (macOS APFS, WSL DrvFs)")

    pty_session_cwd = target
    new("ava-case-alias", pty_session_cwd)
    hits = find_live_anchors(alias, run=unit_home / "run")
    assert any("pty session" in h for h in hits), hits

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=target, start_new_session=True
    )
    try:
        time.sleep(0.5)
        hits = find_live_anchors(alias, run=tmp_path / "nope")
        assert any("process" in h for h in hits), hits
    finally:
        sleeper.kill()
        sleeper.wait()
