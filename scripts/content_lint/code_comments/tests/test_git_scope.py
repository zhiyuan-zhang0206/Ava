import subprocess
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from scripts.content_lint.code_comments.changes import incremental_notes, snapshot
from scripts.content_lint.code_comments.check import Repository, check
from scripts.content_lint.code_comments.cli import main


def run(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return run_bounded(list(argv), cwd=root, timeout=20, text=True, capture_output=True)


def git(root: Path, *args: str) -> str:
    result = run(root, "git", *args)
    result.check_returncode()
    return result.stdout.strip()


def initialize(root: Path, text: str = "value = 1\n") -> Repository:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "comment-check@example.invalid")
    git(root, "config", "user.name", "comment-check")
    (root / "core.py").write_bytes(text.encode("utf-8"))
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return Repository(root, frozenset({"core.py"}))


def test_cli_code_only_edit_keeps_existing_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    initialize(tmp_path, '# Old explanation\ndef f():\n    """Old help."""\n    return 1\n')
    source = tmp_path / "core.py"
    source.write_text(source.read_text().replace("return 1", "return 2"))
    git(tmp_path, "add", ".")
    assert main(["--repo", str(tmp_path), "changed"]) == 0
    assert "0 new or rewritten notes" in capsys.readouterr().out
    assert main(["--repo", str(tmp_path), "check"]) == 1


@pytest.mark.parametrize("committed", [False, True])
def test_mismatched_target_snapshot_fails_explicitly(tmp_path: Path, committed: bool) -> None:
    repo = initialize(tmp_path)
    source = tmp_path / "core.py"
    source.write_text("value = 2\n")
    if committed:
        git(tmp_path, "add", ".")
    with pytest.raises(ValueError, match="snapshot mismatch"):
        snapshot(repo, "HEAD" if committed else None)


def test_full_push_range_catches_earlier_new_prose(tmp_path: Path) -> None:
    repo = initialize(tmp_path)
    base = git(tmp_path, "rev-parse", "HEAD")
    source = tmp_path / "core.py"
    source.write_text("# Added prose\nvalue = 2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "new prose")
    source.write_text("# Added prose\nvalue = 3\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "code only")
    notes = incremental_notes(repo, from_ref=base, to_ref="HEAD")
    assert [
        f.code for f in check(repo, strict_paths=frozenset(notes), strict_notes=notes).findings
    ] == ["free-comment"]
    with pytest.raises(ValueError, match="checked-out HEAD"):
        snapshot(repo, from_ref=base, to_ref=base)
    with pytest.raises(ValueError, match="Git comparison failed"):
        snapshot(repo, "missing-base")


@pytest.mark.parametrize("rename", [False, True])
def test_copy_and_rename_targets_cannot_borrow_another_path(tmp_path: Path, rename: bool) -> None:
    initialize(tmp_path, "# Old prose\nvalue = 1\n")
    source = tmp_path / "core.py"
    target = tmp_path / "renamed.py"
    target.write_bytes(source.read_bytes())
    if rename:
        source.unlink()
    git(tmp_path, "add", ".")
    files = frozenset({"renamed.py"} if rename else {"core.py", "renamed.py"})
    repo = Repository(tmp_path, files)
    notes = incremental_notes(repo)
    assert [
        f.path for f in check(repo, strict_paths=frozenset(notes), strict_notes=notes).findings
    ] == ["renamed.py"]


@pytest.mark.parametrize("symlink_first", [False, True])
def test_file_type_changes_never_supply_legacy_note_credit(
    tmp_path: Path, symlink_first: bool
) -> None:
    repo = initialize(tmp_path, "# Old prose\nvalue = 1\n")
    source = tmp_path / "core.py"
    (tmp_path / "target").write_text("value = 1\n")
    if symlink_first:
        source.unlink()
        source.symlink_to("target")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "symlink base")
    source.unlink()
    if symlink_first:
        source.write_text("# Old prose\nvalue = 2\n")
        git(tmp_path, "add", ".")
        assert incremental_notes(repo) == {"core.py": frozenset({0})}
    else:
        source.symlink_to("target")
        git(tmp_path, "add", ".")
        with pytest.raises(ValueError, match="regular"):
            snapshot(repo)


def test_crlf_current_and_previous_notes_both_come_from_git_blobs(tmp_path: Path) -> None:
    text = 'def f():\r\n    """First.\r\n    Second.\r\n    """\r\n    return 1\r\n'
    repo = initialize(tmp_path, text)
    (tmp_path / "core.py").write_bytes(text.replace("return 1", "return 2").encode())
    git(tmp_path, "add", ".")
    assert incremental_notes(repo) == {"core.py": frozenset()}


@pytest.mark.parametrize("broken_base", [False, True])
def test_parse_failure_on_either_side_does_not_become_empty_scope(
    tmp_path: Path, broken_base: bool
) -> None:
    repo = initialize(tmp_path, "def broken(\n" if broken_base else "value = 1\n")
    (tmp_path / "core.py").write_text("value = 1\n" if broken_base else "def broken(\n")
    git(tmp_path, "add", ".")
    with pytest.raises(SyntaxError):
        incremental_notes(repo)


def test_empty_staged_scope_is_not_whole_tree_migration(tmp_path: Path) -> None:
    repo = initialize(tmp_path, "# Old notes\n")
    assert incremental_notes(repo) == {}
