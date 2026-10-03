"""Module-move audits cover old dotted imports and exact source-file paths."""

import pytest

from scripts.audit import module_moves as gate


def test_old_references_reports_dotted_parent_import_and_slash_lines() -> None:
    text = (
        "import pkg_old.mod_name\n"
        "from pkg_old import mod_name\n"
        "See pkg_old/mod_name.py for details.\n"
    )

    assert gate._old_references("pkg_old.mod_name", text) == [1, 2, 3]


def test_mask_history_reads_blanks_quoted_and_bare_operands() -> None:
    old_path = "base/" + "pty_sessions"
    text = (
        f'git show "$SHA:{old_path}/cli.py" > base/sessions/pty/cli.py\n'
        f"git show bd6b15ed0:{old_path}/host.py > base/sessions/pty/host.py\n"
    )

    masked = gate._mask_history_reads(text)

    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")
    assert old_path not in masked
    assert masked.count(" > base/sessions/pty/") == 2


def test_old_references_ignores_git_show_history_reads_but_reports_old_destination() -> None:
    old_path = "base/" + "pty_sessions/cli.py"
    old_module = "base." + "pty_sessions.cli"
    quoted = f'git show "abc:{old_path}" > base/sessions/pty/cli.py'
    bare = f"git show bd6b15ed0:{old_path} > base/sessions/pty/cli.py"
    old_destination = f'git show "abc:{old_path}" > {old_path}'

    assert gate._old_references(old_module, quoted) == []
    assert gate._old_references(old_module, bare) == []
    assert gate._old_references(old_module, old_destination) == [1]


@pytest.mark.parametrize(
    "text",
    [
        "pkg_old/sub/mod_name.py",
        "unrelated text",
        "mod_name.py",
        "pkg_old/mod_name.pyc",
        "pkg_old/mod_name.pyi",
        "other_pkg_old/mod_name.py",
        "other.pkg_old/mod_name.py",
    ],
)
def test_old_references_ignores_unrelated_or_inexact_paths(text: str) -> None:
    assert gate._old_references("pkg_old.mod_name", text) == []


def test_is_excluded_covers_the_frozen_axes_only() -> None:
    assert gate._is_excluded("") is True
    assert gate._is_excluded("decisions/x.md") is True
    assert gate._is_excluded("postmortems/y.md") is True
    assert gate._is_excluded("db/schema.sql") is False
    assert gate._is_excluded("base/packages/docs/notes.py") is False


def test_missing_accepts_submodule_fallback() -> None:
    """`from pkg import sub` resolves for a lean package __init__ (no re-exports)."""
    assert gate._missing("base.agents", {"history"}) == []
    assert gate._missing("base.agents.history", {"timeline"}) == []


def test_missing_reports_unknown_names() -> None:
    assert gate._missing("base.agents", {"no_such_name_xyz"}) == ["no_such_name_xyz"]
