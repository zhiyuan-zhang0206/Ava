import json
from pathlib import Path

import pytest

from scripts.content_lint.code_comments.cli import main, selected_paths
from scripts.content_lint.code_comments.tests.cases import (
    DOC,
    files,
    git_repository,
    note,
    repository,
)


def cli(root: Path, *argv: str) -> int:
    return main(["--repo", str(root), *argv])


def test_an_explicit_check_selects_the_existing_prose(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"legacy.py": "# Old notes\n"})
    assert selected_paths(repo, []) == frozenset({"legacy.py"})


def test_a_directory_selection_keeps_only_source_files(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        {"pkg/core.py": "value = 1\n", "pkg/notes.md": "# Notes\n", "other.py": "value = 1\n"},
    )
    assert selected_paths(repo, ["pkg"]) == frozenset({"pkg/core.py"})


def test_an_unsupported_explicit_source_does_not_claim_success(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.rs": "// Rust\n"})
    with pytest.raises(ValueError, match="no source parser"):
        selected_paths(repo, ["core.rs"])


def test_validate_checks_links_but_not_prose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = files()
    source["pkg/legacy.py"] = "# Unmigrated prose.\n"
    git_repository(tmp_path, source)
    assert cli(tmp_path, "validate") == 0
    assert "links only" in capsys.readouterr().out
    assert cli(tmp_path, "inventory") == 1
    assert "free-comment" in capsys.readouterr().out


def test_check_reports_every_finding_and_its_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = files()
    source["pkg/core.py"] = source["pkg/core.py"].replace(DOC, "pkg/docs/missing.md")
    source["pkg/legacy.py"] = "# Unmigrated prose.\n"
    git_repository(tmp_path, source)
    assert cli(tmp_path, "check") == 1
    output = capsys.readouterr().out
    assert "pkg/core.py:1: [note-link] not an exact tracked path: pkg/docs/missing.md" in output
    assert "pkg/legacy.py:1: [free-comment]" in output
    assert "code comments: 1 notes; 3 source files checked for free comments" in output


def test_the_json_report_exposes_the_scope_and_the_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=("pkg/docs/missing.md",)) + (
        "def budget() -> int:\n    return 1\n"
    )
    git_repository(tmp_path, source)
    assert cli(tmp_path, "--json", "validate") == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "mode": "validate",
        "notes": 1,
        "strict_source_files": 0,
        "new_or_rewritten_notes": None,
        "findings": [
            {
                "path": "pkg/core.py",
                "line": 1,
                "code": "note-link",
                "message": "not an exact tracked path: pkg/docs/missing.md",
            }
        ],
    }


def test_corpus_extracts_the_notes_in_both_formats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_repository(tmp_path, files())
    assert cli(tmp_path, "corpus") == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows == [
        {
            "line": 1,
            "links": [DOC],
            "owner": "budget",
            "path": "pkg/core.py",
            "text": "The budget stays positive.",
        }
    ]
    assert cli(tmp_path, "corpus", "--format", "markdown") == 0
    assert "## Notes" in capsys.readouterr().out
    output = tmp_path / "corpus.md"
    assert cli(tmp_path, "corpus", "--format", "markdown", "--output", str(output)) == 0
    assert "written to" in capsys.readouterr().err
    assert "## Repeated text" in output.read_text(encoding="utf-8")


def test_unknown_targets_fail_without_a_claim_of_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_repository(tmp_path, files())
    assert cli(tmp_path, "check", "missing.py") == 2
    assert "no tracked files under missing.py" in capsys.readouterr().err
    assert cli(tmp_path, "check", DOC) == 2
    assert "no source parser" in capsys.readouterr().err


def test_corpus_reports_broken_links_on_stderr_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = files()
    source["pkg/core.py"] = note(
        ("The budget stays positive.",), links=("pkg/docs/missing.md",)
    ) + ("def budget() -> int:\n    return 1\n")
    git_repository(tmp_path, source)
    for argv in (("corpus", "--format", "jsonl"), ("corpus", "--format", "markdown")):
        assert cli(tmp_path, *argv) == 1
        captured = capsys.readouterr()
        assert "note-link" in captured.err
        assert "note-link" not in captured.out
