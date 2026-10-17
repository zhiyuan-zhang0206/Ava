from pathlib import Path

import pytest

from scripts.content_lint import lint_skill_md_size as lint


def _write(repo: Path, rel: str, lines: int) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n" * lines, encoding="utf-8")
    return path


def test_a_skill_at_or_below_the_floor_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/skills/a/b/SKILL.md", 250)
    assert lint.main(repo_root=tmp_path) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_transitional_zone_prints_one_note_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/skills/a/b/SKILL.md", 251)
    assert lint.main(repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "note:" in out
    assert "251 lines" in out
    assert "error:" not in out


def test_the_hard_ceiling_itself_still_only_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/skills/a/b/SKILL.md", 300)
    assert lint.main(repo_root=tmp_path) == 0
    assert "note:" in capsys.readouterr().out


def test_one_line_past_the_hard_ceiling_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/skills/a/b/SKILL.md", 301)
    assert lint.main(repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "301 lines" in out


def test_a_three_deep_sub_skill_is_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/skills/a/b/c/SKILL.md", 301)
    assert lint.main(repo_root=tmp_path) == 1
    assert "a/b/c/SKILL.md" in capsys.readouterr().out


def test_plugin_and_project_skills_are_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "meiva_builtins/plugins/x/skills/SKILL.md", 301)
    _write(tmp_path, ".agents/skills/y/SKILL.md", 301)
    assert lint.main(repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "plugins/x/skills/SKILL.md" in out
    assert ".agents/skills/y/SKILL.md" in out


def test_a_skill_reachable_through_a_linked_tree_is_reported_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = _write(tmp_path, "meiva_builtins/skills/a/b/SKILL.md", 301)
    link = tmp_path / ".agents" / "skills" / "a"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real.parent.parent, target_is_directory=True)
    assert lint.main(repo_root=tmp_path) == 1
    assert capsys.readouterr().out.count("301 lines") == 1
