"""Public entry contract for the repository language gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.content_lint import lint_no_cjk as gate


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Use a real Git index for default scans without changing the checkout."""
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


def _write(repo: Path, rel: str, content: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    "character", ["\u4e2d", "\u3400", "\uf900", "\u3059", "\u30ad", "\uc778", "\uff0c", "\u300c"]
)
def test_cjk_line_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str], character: str
) -> None:
    _write(repo, "docs/guide.md", f"# Guide\n  {character} text  \n")
    assert gate.main(["docs/guide.md"], repo_root=repo) == 1
    output = capsys.readouterr()
    assert (
        output.out == f"docs/guide.md:2: U+{ord(character):04X} {character!r} | {character} text\n"
    )
    assert "Raw CJK found" in output.err


@pytest.mark.parametrize(
    "content",
    ["All English.\n", r"functional = '\u4e2d'", "\x00\u4e2d", "\u4e2d".encode() + b"\xff"],
)
def test_clean_or_binary_file_passes(
    repo: Path, capsys: pytest.CaptureFixture[str], content: str | bytes
) -> None:
    _write(repo, "assets/data.txt", content)
    assert gate.main(["assets/data.txt"], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "rel",
    [
        "ui/web/messages/zh/interface/common.json",
        "frontend/locales/zh/app.json",
        "frontend/zh.po",
        "base/telemetry/alerts/copy.py",
        "base/packages/docs/pages_copy.py",
    ],
)
def test_documented_locale_copy_is_exempt(
    repo: Path, capsys: pytest.CaptureFixture[str], rel: str
) -> None:
    _write(repo, rel, "\u4fdd\u5b58\n")
    assert gate.main([rel], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "rel",
    [
        "ava_builtins/skills/pkg/SKILL.md",
        "base/other/copy.py",
        "docs/messages.md",
        "docs/decisions/history.md",
    ],
)
def test_non_locale_content_is_scanned(repo: Path, rel: str) -> None:
    _write(repo, rel, "\u4e2d\n")
    assert gate.main([rel], repo_root=repo) == 1


def test_default_scan_uses_git_index_order_and_reports_each_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/z.md", "clean\n  \u4e2d first  \n\u6587 second\n")
    _write(repo, "docs/a.md", "\u65e5 later\n")
    _write(repo, "untracked.txt", "\u4e2d ignored\n")
    subprocess.run(["git", "add", "docs/z.md", "docs/a.md"], cwd=repo, check=True)
    assert gate.main([], repo_root=repo) == 1
    assert capsys.readouterr().out == (
        "docs/a.md:1: U+65E5 '\u65e5' | \u65e5 later\n"
        "docs/z.md:2: U+4E2D '\u4e2d' | \u4e2d first\n"
        "docs/z.md:3: U+6587 '\u6587' | \u6587 second\n"
    )
    _write(repo, "docs/a.md", "clean\n")
    _write(repo, "docs/z.md", "clean\n")
    assert gate.main([], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


def test_explicit_targets_scan_untracked_sorted_deduplicated(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/z.md", "\u4e2d\n")
    _write(repo, "docs/a.md", "\u6587\n")
    assert gate.main(["docs/z.md", "docs"], repo_root=repo) == 1
    assert capsys.readouterr().out == (
        "docs/a.md:1: U+6587 '\u6587' | \u6587\ndocs/z.md:1: U+4E2D '\u4e2d' | \u4e2d\n"
    )


def test_missing_target_fails_before_scanning_existing_targets(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "bad.txt", "\u4e2d\n")
    for args in (["typo.txt"], [str(repo / "typo.txt")], ["bad.txt", "typo.txt"]):
        assert gate.main(args, repo_root=repo) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert "target path(s) not found" in output.err
        assert "typo.txt" in output.err


def test_outside_directory_is_scanned(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    _write(outside, "data.txt", "English\n")
    assert gate.main([str(outside)], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")
    _write(outside, "data.txt", "\u4e2d\n")
    assert gate.main([str(outside)], repo_root=repo) == 1
    assert capsys.readouterr().out == f"{outside}/data.txt:1: U+4E2D '\u4e2d' | \u4e2d\n"


def test_only_scope_and_tooling_widening(repo: Path) -> None:
    _write(repo, "docs/bad.md", "\u4e2d\n")
    _write(repo, "docs/clean.md", "clean\n")
    _write(repo, "scripts/content_lint/tool.py", "")
    subprocess.run(["git", "add", "docs", "scripts"], cwd=repo, check=True)
    assert gate.main(["--only", "docs/bad.md"], repo_root=repo) == 1
    assert gate.main(["--only", "docs/clean.md"], repo_root=repo) == 0
    assert gate.main(["--only"], repo_root=repo) == 0
    assert (
        gate.main(["--only", "docs/clean.md", "scripts/content_lint/tool.py"], repo_root=repo) == 1
    )


def test_unknown_only_target_is_rejected_before_explicit_scan(repo: Path) -> None:
    _write(repo, "docs/clean.md", "clean\n")
    with pytest.raises(SystemExit, match=r"not found: typo\.txt"):
        gate.main(["docs/clean.md", "--only", "typo.txt"], repo_root=repo)
