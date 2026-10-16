from pathlib import Path

import pytest

from scripts.lint import agents_md_size as lint


def _root_with(tmp_path: Path, lines: int) -> Path:
    (tmp_path / "AGENTS.md").write_text("x\n" * lines, encoding="utf-8")
    return tmp_path


def test_a_file_at_or_below_the_floor_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 200)) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_transitional_zone_prints_one_note_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 201)) == 0
    out = capsys.readouterr().out
    assert "note:" in out
    assert "201 lines" in out
    assert "error:" not in out


def test_the_hard_ceiling_itself_still_only_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 245)) == 0
    assert "note:" in capsys.readouterr().out


def test_one_line_past_the_hard_ceiling_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 246)) == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "246 lines" in out


def test_a_missing_agents_md_fails_loud(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert lint.main(repo_root=tmp_path) == 1
    assert "not found" in capsys.readouterr().err
