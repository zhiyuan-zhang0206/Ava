"""Declared runtime inputs pass outside targets and reject undeclared repository targets."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.packages.declared_inputs import (
    REPOSITORY_ROOT,
    declared_import,
    declared_path,
    declared_spec,
    matches,
    supplied_path,
)


@pytest.mark.parametrize(
    ("parts", "pattern", "expected"),
    [
        ("a.b.c", "a.*.c", True),
        ("a.b.x.c", "a.*.c", False),
        ("a.b.x.c", "a.**.c", True),
        ("a.c", "a.**.c", True),
        ("a.b", "a.b.**", True),
        ("a.bc", "a.b", False),
    ],
)
def test_segment_patterns(parts: str, pattern: str, expected: bool) -> None:
    assert matches(parts.split("."), pattern.split(".")) is expected


def test_import_inside_its_domain_loads_the_module() -> None:
    module = declared_import("base.packages.declared_inputs", within=("base.packages.*",))
    assert module.REPOSITORY_ROOT == REPOSITORY_ROOT


def test_repository_import_outside_its_domain_is_refused() -> None:
    with pytest.raises(ValueError, match="outside its declared domain"):
        declared_import("base.packages.declared_inputs", within=("ava.*",))
    with pytest.raises(ValueError, match="outside its declared domain"):
        declared_spec("base.packages.declared_inputs", within=())


def test_non_repository_modules_pass_through() -> None:
    assert declared_import("json", within=()).__name__ == "json"
    assert declared_spec("definitely_not_a_module_xyz", within=()) is None


def test_paths_outside_the_checkout_pass_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "state.pid"
    assert declared_path(target) is not None
    assert declared_path(str(target)) == target


def test_repository_paths_must_match_their_domain() -> None:
    own = Path(__file__)
    relative = own.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    assert declared_path(own, within=("base/**/test_*.py",)) == own
    assert declared_path(own, within=(relative,)) == own
    with pytest.raises(ValueError, match="outside its declared domain"):
        declared_path(own)
    with pytest.raises(ValueError, match="outside its declared domain"):
        declared_path(own, within=("ava/**",))


def test_tooling_directories_are_not_repository_inputs() -> None:
    assert declared_path(REPOSITORY_ROOT / ".venv" / "pyvenv.cfg") is not None


def test_supplied_paths_pass_through_even_inside_the_checkout() -> None:
    assert supplied_path(__file__) == Path(__file__)
