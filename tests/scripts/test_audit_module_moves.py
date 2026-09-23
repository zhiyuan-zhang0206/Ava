"""Module-move audits cover old dotted imports and exact source-file paths."""

import pytest

from scripts import audit_module_moves as gate


def test_old_references_reports_dotted_parent_import_and_slash_lines() -> None:
    text = (
        "import pkg_old.mod_name\n"
        "from pkg_old import mod_name\n"
        "See pkg_old/mod_name.py for details.\n"
    )

    assert gate._old_references("pkg_old.mod_name", text) == [1, 2, 3]


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


def test_is_excluded_covers_frozen_axes_and_hash_pinned_fixture() -> None:
    assert gate._is_excluded("") is True
    assert gate._is_excluded("decisions/x.md") is True
    assert gate._is_excluded("postmortems/y.md") is True
    assert gate._is_excluded("docs/history/2026/z.md") is True
    assert gate._is_excluded("scripts/legacy_lkg/compatibility.patch") is True
    assert gate._is_excluded("scripts/legacy_lkg/prepare.py") is False
    assert gate._is_excluded("shared/docs/notes.py") is False
