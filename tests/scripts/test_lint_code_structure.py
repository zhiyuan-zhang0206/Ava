"""scripts/lint_code_structure.py — the per-file line budget.

Locks the structural rules: a file past the 800-line hard ceiling is a hard
error with no exemption, and files in the 600-800 transitional zone surface
as non-blocking notes.
"""

from __future__ import annotations

import pathlib

from scripts import lint_code_structure as lcs


def _write(tmp_path: pathlib.Path, name: str, n_lines: int) -> pathlib.Path:
    p = tmp_path / name
    p.write_text("x = 1\n" * n_lines, encoding="utf-8")
    return p


def _scan(tmp_path: pathlib.Path, name: str, n_lines: int) -> list[str]:
    p = _write(tmp_path, name, n_lines)
    return [msg for _ln, msg, sev in lcs._scan_file(p, name) if sev == "error"]


def test_over_ceiling_is_hard_error(tmp_path: pathlib.Path) -> None:
    """An over-800 file is a hard error — no allowlist remains."""
    errors = _scan(tmp_path, "big.py", 900)
    assert any("hard ceiling" in e for e in errors)


def test_transitional_zone_is_note_not_error(tmp_path: pathlib.Path) -> None:
    """601-800 lines is a non-blocking note; exactly the floor is silent."""
    p = _write(tmp_path, "mid.py", 700)
    results = lcs._scan_file(p, "mid.py")
    assert any(sev == "note" for _ln, _msg, sev in results)
    assert not any(sev == "error" for _ln, _msg, sev in results)

    p2 = _write(tmp_path, "floor.py", lcs._TRANSITIONAL_FLOOR)
    assert lcs._scan_file(p2, "floor.py") == []
