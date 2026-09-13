"""scripts/lint_code_structure.py — the per-file line budget.

Locks the structural rules: a file past the 800-line hard ceiling is a hard
error with no exemption, and files in the 600-800 transitional zone surface
as non-blocking notes.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts import lint_code_structure as lcs


def _write(tmp_path: pathlib.Path, name: str, n_lines: int) -> pathlib.Path:
    p = tmp_path / name
    p.write_text("x = 1\n" * n_lines, encoding="utf-8")
    return p


def _scan(tmp_path: pathlib.Path, name: str, n_lines: int) -> list[str]:
    p = _write(tmp_path, name, n_lines)
    return [msg for _ln, msg, sev in lcs._scan_file(p, name) if sev == "error"]


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable *.py member (non-UTF-8 / dangling symlink) must be skipped
    like any unreadable entry — the scan must not crash on it, in any of its
    call forms (file / directory / default)."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (shared / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (shared / "dangling.py").symlink_to(shared / "missing.py")
    assert lcs.main([str(shared / "bad_utf8.py")]) == 0
    assert lcs.main([str(shared)]) == 0
    assert lcs.main([]) == 0


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


def test_explicit_missing_target_is_an_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = _write(tmp_path, "ok.py", 1)
    missing = tmp_path / "typo.py"
    assert lcs.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert lcs.main([str(good), str(missing)]) == 1
