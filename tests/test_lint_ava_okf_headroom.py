"""`scripts/lint_ava_okf.py` — the W010 headroom warning (rule 10).

Drives `main()` against a tmp tree used as the linter's repo root, so nothing
escapes into the real doc graph. The assertions cover the property that makes
W010 useful — it reports and does **not** block — by checking the effective exit
code, which is the only way to catch a warning that quietly became an error: a
W010 promoted to E-severity would still print the same message.

The band boundary is exercised from both sides so a sign slip in the comparison
cannot pass, and the thresholds are read off the module rather than hardcoded so
a bump to either one moves the test with it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# The linter imports build_okf_data as a top-level module — running it by path
# puts scripts/ on sys.path[0], so importing it here has to do the same.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
_lint = importlib.import_module("scripts.lint_ava_okf")

MAX_CHARS: int = _lint.MAX_CHARS
WARN_MARGIN: int = _lint.WARN_MARGIN

_FRONTMATTER = """---
type: doc
title: Sized Node
description: A node built to an exact character count.
---

# Sized Node

"""


def _node_of_size(tmp_path, char_count: int) -> None:
    """Write an otherwise-valid OKF node whose decoded length is exactly
    `char_count`, padded with prose-shaped filler so no other rule can fire."""
    filler_len = char_count - len(_FRONTMATTER) - 1  # -1 for the trailing newline
    assert filler_len > 0, "char_count too small to hold the frontmatter"
    # One long filler line: MAX_LINES is not the rule under test, and a single
    # line keeps the node far away from it.
    body = (" ".join(["word"] * (filler_len // 5 + 1)))[:filler_len]
    path = tmp_path / "sized.ava.okf.md"
    path.write_text(_FRONTMATTER + body + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8")) == char_count


def _lint_tmp(tmp_path, monkeypatch, capsys) -> tuple[int, str]:
    """Lint `tmp_path` as the repo root; return (effective exit code, stdout)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py"])
    code = 0
    try:
        _lint.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


def test_warns_inside_the_margin_without_blocking(tmp_path, monkeypatch, capsys):
    """One character into the band: W010 reports the size and the room left, and
    the linter still exits clean."""
    size = MAX_CHARS - WARN_MARGIN + 1
    _node_of_size(tmp_path, size)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W010" in out
    assert str(size) in out  # its size
    assert str(WARN_MARGIN - 1) in out  # the room left
    assert "1 warning(s)" in out
    assert "0 error(s)" in out


def test_silent_at_the_margin_boundary(tmp_path, monkeypatch, capsys):
    """Room left exactly equal to the margin is not yet in the band — the warning
    fires on *less* room than the margin, not on the margin itself."""
    _node_of_size(tmp_path, MAX_CHARS - WARN_MARGIN)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W010" not in out
    assert "0 warning(s)" in out


@pytest.mark.parametrize("headroom", [WARN_MARGIN + 1, WARN_MARGIN * 2, MAX_CHARS // 2])
def test_silent_well_below_the_margin(tmp_path, monkeypatch, capsys, headroom: int):
    _node_of_size(tmp_path, MAX_CHARS - headroom)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W010" not in out


def test_over_the_ceiling_errors_and_does_not_also_warn(tmp_path, monkeypatch, capsys):
    """Past the ceiling E007 is the only report: a breach already names the
    remedy, so W010 on top of it would be duplicate noise."""
    _node_of_size(tmp_path, MAX_CHARS + 1)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1
    assert "E007" in out
    assert "W010" not in out
