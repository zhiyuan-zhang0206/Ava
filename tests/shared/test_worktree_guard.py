"""Tests for shared/worktree_guard.py — the `git worktree remove` guard (issue #194)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from shared.worktree_guard import find_live_anchors


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
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=target)
    try:
        time.sleep(0.5)  # let the child start so psutil sees its cwd
        hits = find_live_anchors(target, records_dir=tmp_path / "nope")
        assert any("process" in h and str(target) in h for h in hits)
    finally:
        child.kill()
        child.wait()
