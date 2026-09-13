"""`scripts/lint_no_plugin_wrap.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to scan nothing and exit 0;
it must now report the missing target on stderr and exit 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_no_plugin_wrap as gate


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable *.py member under plugins/ (non-UTF-8 / dangling symlink)
    must be skipped like any unreadable entry — the scan must not crash on it,
    and a violating sibling file is still reported."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (plugins / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (plugins / "dangling.py").symlink_to(plugins / "missing.py")
    assert gate.main([str(plugins / "bad_utf8.py")]) == 0
    assert gate.main([]) == 0
    (plugins / "viol.py").write_text("ava.files.read = my_read\n", encoding="utf-8")
    assert gate.main([]) == 1
    assert "ava.files.read" in capsys.readouterr().out


def test_explicit_plugins_directory_argument_enumerates_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plugins/ directory itself is in scope, so a directory argument
    enumerates its members (it used to be filtered out: an empty scan, rc 0)."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "ok.py").write_text("value = 1\n", encoding="utf-8")
    assert gate.main([str(plugins)]) == 0
    (plugins / "viol.py").write_text("ava.files.read = my_read\n", encoding="utf-8")
    assert gate.main([str(plugins)]) == 1
    assert "ava.files.read" in capsys.readouterr().out


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
