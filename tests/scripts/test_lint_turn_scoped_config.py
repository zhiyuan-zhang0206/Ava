"""`scripts/lint_turn_scoped_config.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to scan nothing and exit 0;
it must now report the missing target on stderr and exit 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_turn_scoped_config as gate


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


def test_explicit_outside_repo_target_scans_cleanly(tmp_path: Path) -> None:
    """An existing path outside the repo scans instead of dying on the
    repo-relative prefix computation."""
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    assert gate.main([str(outside)]) == 0
