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
