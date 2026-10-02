"""Tests for base/deploy/git/worktree_guard.py — the `git worktree remove` guard (issue #194)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from base.deploy.git.worktree_guard import find_live_anchors


def test_session_record_anchor_reported(tmp_path: Path) -> None:
    target = tmp_path / "worktrees" / "wt-under-test"
    pty = tmp_path / "pty"
    pty.mkdir()
    (pty / "ava-schedule-1.json").write_text(
        json.dumps({"pid": 4242, "cwd": str(target), "cmd": "/bin/bash -l -i"})
    )
    (pty / "agent-shell.json").write_text(
        json.dumps({"pid": 4243, "cwd": str(tmp_path / "workspaces" / "9"), "cmd": "/bin/bash"})
    )
    hits = find_live_anchors(target, records_dir=pty)
    assert len(hits) == 1
    assert "ava-schedule-1" in hits[0] and str(target) in hits[0]


def test_clean_when_nothing_anchored(tmp_path: Path) -> None:
    target = tmp_path / "worktrees" / "wt-under-test"
    pty = tmp_path / "pty"
    pty.mkdir()
    (pty / "agent-shell.json").write_text(
        json.dumps({"pid": 1, "cwd": str(tmp_path / "workspaces" / "9"), "cmd": "/bin/bash"})
    )
    assert find_live_anchors(target, records_dir=pty) == []


def test_malformed_record_skipped(tmp_path: Path) -> None:
    pty = tmp_path / "pty"
    pty.mkdir()
    (pty / "broken.json").write_text("{not json")
    assert find_live_anchors(tmp_path / "worktrees" / "wt", records_dir=pty) == []


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
        hits = find_live_anchors(target, records_dir=tmp_path / "nope")
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


def test_case_alias_spelling_finds_anchors(tmp_path: Path) -> None:
    """#3707 QA (fail-open): on a case-insensitive filesystem a differently
    spelled target — `.../ava/...` from bash's logical pwd — names the same
    directory as the physical spelling psutil / the records report
    (`.../Ava/...`). The containment check must compare identity, not resolved
    strings: with the old string compare both arms below missed the anchor."""
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    alias = _case_alias(target)
    if alias is None:
        pytest.skip("requires a case-insensitive filesystem (macOS APFS, WSL DrvFs)")

    pty = tmp_path / "pty"
    pty.mkdir()
    (pty / "sess.json").write_text(
        json.dumps({"pid": 4242, "cwd": str(target), "cmd": "/bin/bash"})
    )
    hits = find_live_anchors(alias, records_dir=pty)
    assert any("pty session" in h for h in hits), hits

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=target, start_new_session=True
    )
    try:
        time.sleep(0.5)
        hits = find_live_anchors(alias, records_dir=tmp_path / "nope")
        assert any("process" in h for h in hits), hits
    finally:
        sleeper.kill()
        sleeper.wait()
