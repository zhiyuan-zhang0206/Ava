"""`scripts/lint_no_os_environ.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to scan nothing and exit 0;
it must now report the missing target on stderr and exit 1. An unreadable
member of an explicit directory (e.g. a dangling symlink) is skipped, not a
crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_no_os_environ as gate


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)]) == 1


def test_directory_with_dangling_symlink_member_is_skipped(tmp_path: Path) -> None:
    """A broken *.py symlink inside an explicit directory must be skipped like
    any unreadable entry — the scan must not crash on it."""
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert gate.main([str(tmp_path)]) == 0
