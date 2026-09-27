"""Contract snapshots: `scripts/structure/contracts.py` renders a door's public
surface (Rule 4's flip side — what a package DOES promise, not what it hides).

Unit coverage of `_module_entries` against synthetic sources (rendering forms,
private exclusions), then the `--write`/`--check` gate lifecycle through
`contracts.main()` against a synthetic door under `tmp_path`, and finally one
end-to-end check against the real repo's committed snapshots.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import contracts

# --- unit: _module_entries rendering --------------------------------------------


def _entries(source: str, rel_path: str = "pkg/mod.py") -> dict[str, list[str]]:
    tree = ast.parse(source)
    return dict(contracts._module_entries(tree, rel_path))


def test_function_rendering() -> None:
    entries = _entries("def foo(a: int, b: str = 'x') -> bool:\n    return True\n")
    assert entries == {"foo": ["def foo(a: int, b: str='x') -> bool"]}


def test_async_function_rendering() -> None:
    entries = _entries("async def foo(x: int) -> None:\n    pass\n")
    assert entries == {"foo": ["async def foo(x: int) -> None"]}


def test_decorated_function_rendering() -> None:
    entries = _entries("@some.decorator\ndef foo() -> None:\n    pass\n")
    assert entries == {"foo": ["@some.decorator", "def foo() -> None"]}


def test_class_with_methods_and_fields() -> None:
    source = (
        "class Foo:\n"
        "    x: int\n"
        "    def method(self) -> None: ...\n"
        "    def _private(self) -> None: ...\n"
        "    def __init__(self) -> None: ...\n"
        "    def __init_subclass__(cls) -> None: ...\n"
    )
    entries = _entries(source)
    assert entries["Foo"] == [
        "class Foo",
        "  def __init__(self) -> None",
        "  def method(self) -> None",
        "  x: int",
    ]


def test_enum_like_class_members() -> None:
    source = "class Color(Enum):\n    RED = 'red'\n    BLUE = 'blue'\n"
    entries = _entries(source)
    assert entries["Color"] == [
        "class Color(Enum)",
        "  BLUE = 'blue'",
        "  RED = 'red'",
    ]


def test_class_keyword_bases() -> None:
    source = "class Foo(Base, total=False):\n    x: int\n"
    entries = _entries(source)
    assert entries["Foo"][0] == "class Foo(Base, total=False)"


def test_nested_class() -> None:
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        x: int\n"
        "    class _Hidden:\n"
        "        y: int\n"
        "    def method(self) -> None: ...\n"
    )
    entries = _entries(source)
    assert entries["Outer"] == [
        "class Outer",
        "  class Inner",
        "    x: int",
        "  def method(self) -> None",
    ]


def test_annotated_constant_with_and_without_value() -> None:
    entries = _entries("X: int = 5\nY: str\n")
    assert entries == {"X": ["X: int = 5"], "Y": ["Y: str"]}


def test_short_literal_vs_ellipsis_module_level() -> None:
    entries = _entries("SHORT = 'a value'\nLONG = compute()\n")
    assert entries == {"SHORT": ["SHORT = 'a value'"], "LONG": ["LONG = ..."]}


def test_class_annassign_default_presence_is_contract() -> None:
    """Whether a dataclass/NamedTuple/TypedDict/pydantic field has a default
    decides whether the constructor argument is required — so it must render
    even when the default itself is not a short literal: `...`, not omitted."""
    source = "class Foo:\n    x: int = compute()\n    y: str = 'short'\n    z: int\n"
    entries = _entries(source)
    assert entries["Foo"] == [
        "class Foo",
        "  x: int = ...",
        "  y: str = 'short'",
        "  z: int",
    ]


def test_type_expression_values_render_in_full() -> None:
    """An old-style alias (Subscript on a typing/builtin generic, or a `|`
    BinOp of such expressions/names) is contract and is never truncated to
    `...`, however long — unlike an ordinary non-literal value."""
    source = (
        "Category = Literal['audit', 'telemetry', 'log']\n"
        "Combined = str | list[dict[str, object]]\n"
        "Plain = int | str\n"
        "NotType = some_call()\n"
    )
    entries = _entries(source)
    assert entries["Category"] == ["Category = Literal['audit', 'telemetry', 'log']"]
    assert entries["Combined"] == ["Combined = str | list[dict[str, object]]"]
    assert entries["Plain"] == ["Plain = int | str"]
    assert entries["NotType"] == ["NotType = ..."]


def test_typealias_annotated_value_renders_in_full_regardless_of_shape() -> None:
    """`: TypeAlias` marks the value itself as contract even when it is not a
    recognized generic shape (e.g. a bare custom sentinel)."""
    entries = _entries("Custom: TypeAlias = some_call()\n")
    assert entries == {"Custom": ["Custom: TypeAlias = some_call()"]}


def test_module_level_annassign_non_literal_default_shows_ellipsis() -> None:
    entries = _entries("X: dict = compute()\n")
    assert entries == {"X": ["X: dict = ..."]}


def test_class_assign_non_literal_falls_back_to_ellipsis() -> None:
    source = "class Foo:\n    A = 1\n    B = compute()\n"
    entries = _entries(source)
    assert entries["Foo"] == ["class Foo", "  A = 1", "  B = ..."]


def test_all_restricted_module() -> None:
    source = (
        "__all__ = ['a', 'c', '_private_but_declared']\n"
        "def a() -> None: ...\n"
        "def b() -> None: ...\n"
        "def _private_but_declared() -> None: ...\n"
        "from pkg.other import c\n"
    )
    entries = _entries(source)
    assert set(entries) == {"a", "c", "_private_but_declared"}
    assert entries["c"] == ["c  (re-export of pkg.other.c)"]


def test_explicit_reexport_absolute() -> None:
    entries = _entries("from pkg.other import thing as thing\n")
    assert entries == {"thing": ["thing  (re-export of pkg.other.thing)"]}


def test_explicit_reexport_relative_import() -> None:
    entries = _entries("from .sibling import thing as thing\n", rel_path="pkg/sub/__init__.py")
    assert entries == {"thing": ["thing  (re-export of pkg.sub.sibling.thing)"]}


def test_import_x_as_x_reexport() -> None:
    entries = _entries("import os as os\n")
    assert entries == {"os": ["os  (re-export of os)"]}


# --- unit: private exclusions ---------------------------------------------------


def test_plain_from_import_is_not_a_reexport() -> None:
    entries = _entries("from pkg.other import helper\n")
    assert entries == {}


def test_leading_underscore_names_are_excluded() -> None:
    entries = _entries("def _hidden() -> None: ...\n_x = 1\n_y: int = 1\n")
    assert entries == {}


def test_explicit_reexport_of_a_private_name_stays_excluded() -> None:
    """`_x as _x` is the redundant-alias idiom, but a leading underscore still
    wins — promotion requires dropping the underscore, per Rule 4."""
    entries = _entries("from pkg.other import _x as _x\n")
    assert entries == {}


def test_door_excludes_private_module_and_subpackage(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/public.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/_helper.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/_internal").mkdir()
    (tmp_path / "pkg/_internal/mod.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/sub").mkdir()
    (tmp_path / "pkg/sub/__init__.py").write_text("", encoding="utf-8")

    modules = set(contracts._door_modules("pkg", tmp_path))
    assert modules == {"pkg/__init__.py", "pkg/public.py", "pkg/sub/__init__.py"}


# --- gate: --write / --check lifecycle through a synthetic door -----------------


@pytest.fixture
def _door(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A synthetic single-module door `pkg.mod` wired to contracts._REPO_ROOT."""
    monkeypatch.setattr(contracts, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "DOORS", (("pkg.mod", "pkg/mod.py", "pkg/mod.api.txt"),))
    (tmp_path / "pkg").mkdir()
    return tmp_path


def _write(root: pathlib.Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_check_fails_on_a_missing_snapshot(_door: pathlib.Path) -> None:
    _write(_door, "pkg/mod.py", "def foo() -> None: ...\n")
    assert contracts.main(["--check"]) == 1


def test_write_then_check_passes(_door: pathlib.Path) -> None:
    _write(_door, "pkg/mod.py", "def foo() -> None: ...\n")
    assert contracts.main(["--write"]) == 0
    assert contracts.main(["--check"]) == 0


def test_body_only_change_leaves_snapshot_unchanged(_door: pathlib.Path) -> None:
    _write(_door, "pkg/mod.py", "def foo(x: int) -> int:\n    return x + 1\n")
    contracts.main(["--write"])
    before = (_door / "pkg/mod.api.txt").read_text(encoding="utf-8")

    _write(_door, "pkg/mod.py", "def foo(x: int) -> int:\n    return x * 2\n")
    assert contracts.main(["--check"]) == 0
    after = (_door / "pkg/mod.api.txt").read_text(encoding="utf-8")
    assert before == after


def test_signature_change_fails_check_and_diff_names_it(
    _door: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_door, "pkg/mod.py", "def foo(x: int) -> int:\n    return x\n")
    contracts.main(["--write"])

    _write(_door, "pkg/mod.py", "def foo(x: int, y: int) -> int:\n    return x\n")
    assert contracts.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "def foo(x: int, y: int) -> int" in output


def test_new_public_name_fails_check(
    _door: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_door, "pkg/mod.py", "def foo() -> None: ...\n")
    contracts.main(["--write"])

    _write(_door, "pkg/mod.py", "def foo() -> None: ...\ndef bar() -> None: ...\n")
    assert contracts.main(["--check"]) == 1
    assert "def bar" in capsys.readouterr().out


def test_removed_public_name_fails_check(
    _door: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_door, "pkg/mod.py", "def foo() -> None: ...\ndef bar() -> None: ...\n")
    contracts.main(["--write"])

    _write(_door, "pkg/mod.py", "def foo() -> None: ...\n")
    assert contracts.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "def bar" in output


def test_alias_literal_value_change_fails_check(
    _door: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A type-expression alias is rendered in full, so a changed member is a
    contract change — not silently absorbed into an unchanged `...`."""
    _write(_door, "pkg/mod.py", "Kind = Literal['a', 'b']\n")
    contracts.main(["--write"])

    _write(_door, "pkg/mod.py", "Kind = Literal['a', 'b', 'c']\n")
    assert contracts.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "Kind = Literal['a', 'b', 'c']" in output


def test_field_default_presence_change_fails_check(
    _door: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field gaining or losing a default changes whether the constructor
    argument is required — that must move the snapshot even though the
    default itself is not a short literal."""
    _write(_door, "pkg/mod.py", "class Foo:\n    x: int\n")
    contracts.main(["--write"])

    _write(_door, "pkg/mod.py", "class Foo:\n    x: int = compute()\n")
    assert contracts.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "x: int = ..." in output


def test_deterministic_output_across_two_runs(_door: pathlib.Path) -> None:
    _write(_door, "pkg/mod.py", "def foo() -> None: ...\nclass Bar:\n    x: int\n")
    first = contracts.render_door("pkg.mod", "pkg/mod.py", _door)
    second = contracts.render_door("pkg.mod", "pkg/mod.py", _door)
    assert first == second
    assert first.endswith("\n") and not first.endswith("\n\n")


# --- end-to-end: the real repo's committed snapshots ----------------------------


def test_check_passes_against_the_real_repo() -> None:
    assert contracts.check_snapshots() == []
