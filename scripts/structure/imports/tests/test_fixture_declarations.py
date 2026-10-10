"""Fixture scope merging and source provenance come from one declarative reader."""

from pathlib import Path

import pytest

from scripts.structure.imports.fixture_scopes import (
    Declaration,
    Scope,
    declarations,
    discover_scopes,
)


def test_declarations_preserve_each_source_before_scopes_are_merged(tmp_path: Path) -> None:
    for directory, path in [("agent", "tests/test_one.py"), ("base", "tests/test_two.py")]:
        target = tmp_path / directory / "path_scopes.toml"
        target.parent.mkdir()
        target.write_text(f'"tests.fixtures.shared" = ["{path}"]\n')
    assert declarations(tmp_path) == (
        Declaration(
            "agent/path_scopes.toml", "tests.fixtures.shared", ("agent/tests/test_one.py",)
        ),
        Declaration("base/path_scopes.toml", "tests.fixtures.shared", ("base/tests/test_two.py",)),
    )
    assert discover_scopes(tmp_path) == {
        "tests.fixtures.shared": Scope(("agent/tests/test_one.py", "base/tests/test_two.py"))
    }


def test_root_source_is_repository_relative_without_changing_scope_contract(tmp_path: Path) -> None:
    (tmp_path / "path_scopes.toml").write_text('"tests.fixtures.shared" = ["."]\n')
    assert declarations(tmp_path)[0].source == "path_scopes.toml"
    assert discover_scopes(tmp_path) == {"tests.fixtures.shared": Scope((".",))}


def test_malformed_declaration_still_fails_at_the_reader(tmp_path: Path) -> None:
    (tmp_path / "path_scopes.toml").write_text('"tests.fixtures.shared" = "tests"\n')
    with pytest.raises(ValueError, match="must be a list of names"):
        declarations(tmp_path)
    with pytest.raises(ValueError, match="must be a list of names"):
        discover_scopes(tmp_path)
