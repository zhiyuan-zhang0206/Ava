from pathlib import Path

import pytest

from scripts.content_lint.code_comments.changes import new_note_indexes, parse_blobs
from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.tests.cases import note, repository

BASE = (
    '"""Module help."""\n'
    "# Legacy prose\n"
    "import math\n"
    "\n"
    "def budget() -> int:\n"
    '    """Budget note."""\n'
    "    value = 1\n"
    "    # Keep a reserve\n"
    "    return value\n"
)


def codes(
    tmp_path: Path, previous: str, current: str, extra: dict[str, str] | None = None
) -> list[str]:
    repo = repository(tmp_path, {"core.py": current, **(extra or {})})
    selected = new_note_indexes(parse_python("core.py", current), parse_python("core.py", previous))
    result = check(repo, strict_paths=repo.files, strict_notes={"core.py": selected})
    return [item.code for item in result.findings]


@pytest.mark.parametrize(
    "current",
    [
        BASE.replace("import math", "import os\nimport math"),
        BASE.replace("value = 1", "value = 2"),
        BASE.replace("value", "balance"),
        BASE.replace("value = 1", "value = (\n        1\n    )"),
    ],
)
def test_unrelated_code_changes_do_not_migrate_legacy_notes(tmp_path: Path, current: str) -> None:
    assert codes(tmp_path, BASE, current) == []


@pytest.mark.parametrize(
    "current",
    [
        BASE.replace("reserve", "buffer"),
        BASE.replace('"""Budget note."""', '"""Budget notes."""'),
        BASE.replace('"""Budget note."""', "'''Budget note.'''"),
        BASE.replace("    # Keep a reserve", "    # Keep a reserve\n    # Keep a reserve"),
        BASE.replace("budget", "new_budget"),
    ],
)
def test_new_rewritten_copied_or_reowned_notes_are_strict(tmp_path: Path, current: str) -> None:
    assert "free-comment" in codes(tmp_path, BASE, current)


def test_a_migrated_note_is_new_but_accepted(tmp_path: Path) -> None:
    migrated = BASE.replace(
        "    # Keep a reserve\n",
        note(("Keeps a reserve for the next round.",), links=("docs/core.md",), indent=4),
    )
    assert codes(tmp_path, BASE, migrated, {"docs/core.md": "# Core\n"}) == []


def test_a_migrated_note_still_validates_its_links(tmp_path: Path) -> None:
    migrated = BASE.replace(
        "    # Keep a reserve\n",
        note(("Keeps a reserve for the next round.",), links=("docs/core.md",), indent=4),
    )
    assert codes(tmp_path, BASE, migrated) == ["note-link"]


def test_same_owner_reordering_consumes_each_old_occurrence_once(tmp_path: Path) -> None:
    old = "def f():\n    # First note\n    # Second note\n    return 1\n"
    current = old.replace("# First note\n    # Second note", "# Second note\n    # First note")
    assert codes(tmp_path, old, current) == []


@pytest.mark.parametrize("keyword", ["def", "class"])
def test_cross_owner_moves_do_not_share_old_note_credit(tmp_path: Path, keyword: str) -> None:
    old = (
        f"{keyword} left{'()' if keyword == 'def' else ''}:\n    # Shared text\n    pass\n"
        f"{keyword} right{'()' if keyword == 'def' else ''}:\n    pass\n"
    )
    current = old.replace("    # Shared text\n", "").rstrip() + "\n    # Shared text\n"
    assert codes(tmp_path, old, current) == ["free-comment"]


DUPLICATE = (
    "if flag:\n    def f():\n        # First note\n        return 1\n"
    "else:\n    def f():\n        # Second note\n        return 2\n"
)


def test_duplicate_declarations_keep_separate_occurrence_credit(tmp_path: Path) -> None:
    assert codes(tmp_path, DUPLICATE, DUPLICATE.replace("return 1", "return 3")) == []
    moved = DUPLICATE.replace("        # First note\n", "").replace(
        "        # Second note", "        # First note\n        # Second note"
    )
    assert codes(tmp_path, DUPLICATE, moved) == ["free-comment"]


def test_duplicate_group_growth_cannot_reuse_an_inserted_ordinal(tmp_path: Path) -> None:
    old = "def f():\n    # First note\n    return 1\n\ndef f():\n    return 2\n"
    new = "def f():\n    # First note\n    return 0\n\n" + old.replace("    # First note\n", "")
    assert codes(tmp_path, old, new) == ["free-comment"]


def test_function_class_collision_and_nested_parents_keep_distinct_identity(
    tmp_path: Path,
) -> None:
    old = "def x():\n    # First note\n    pass\nclass x:\n    pass\n"
    new = old.replace("    # First note\n", "") + "    # First note\n"
    assert codes(tmp_path, old, new) == ["free-comment"]
    nested = (
        "class C:\n    def f(self):\n        # First note\n        pass\n"
        "class C:\n    def f(self):\n        pass\n"
    )
    assert codes(
        tmp_path, nested, nested.replace("        # First note\n", "") + "        # First note\n"
    ) == ["free-comment"]


def test_a_property_accessor_code_change_does_not_migrate_other_notes(tmp_path: Path) -> None:
    old = (
        "class Item:\n    @property\n    def value(self):\n        # Getter note\n        return 1\n"
        "    @value.setter\n    def value(self, value):\n        # Setter note\n"
        "        self._value = value\n"
    )
    assert codes(tmp_path, old, old.replace("return 1", "return 2")) == []


def test_a_rewritten_directive_needs_a_note(tmp_path: Path) -> None:
    old = "def f():\n    return call(1)  # noqa: S603\n"
    assert codes(tmp_path, old, old.replace("S603", "S603, S607")) == ["directive-owner"]


def test_an_unchanged_directive_keeps_its_credit_in_changed_scope(tmp_path: Path) -> None:
    old = "def f():\n    return call(1)  # noqa: S603\n"
    assert codes(tmp_path, old, old.replace("call(1)", "call(2)")) == []


def test_an_inline_module_comment_does_not_borrow_adjacent_function_identity(
    tmp_path: Path,
) -> None:
    old = "import math  # Legacy import\ndef f():\n    return 1\n"
    assert codes(tmp_path, old, old.replace("def f", "def g")) == []


def test_crlf_multiline_notes_use_exact_text(tmp_path: Path) -> None:
    old = 'def f():\r\n    """First line.\r\n    Second line.\r\n    """\r\n    return 1\r\n'
    assert codes(tmp_path, old, old.replace("return 1", "return 2")) == []
    assert codes(tmp_path, old, old.replace("Second", "Changed")) == ["free-comment"]


def test_an_unchanged_note_keeps_validating_its_links(tmp_path: Path) -> None:
    content = note(("Kept.",), links=("docs/core.md",)) + "def f():\n    return None\n"
    current = content.replace("return None", "return 1")
    assert codes(tmp_path, content, current) == ["note-link"]
    assert codes(tmp_path, content, current, {"docs/core.md": "# Core\n"}) == []


@pytest.mark.parametrize(
    ("old", "current"),
    [
        (
            "run(() => { /* Retain */ }); run(() => {});",
            "run(() => {}); run(() => { /* Retain */ });",
        ),
        (
            "const x = { 'a'() { /* Retain */ }, 'b'() {} };",
            "const x = { 'a'() {}, 'b'() { /* Retain */ } };",
        ),
        (
            "const A = class { /* Retain */ }; const B = class {};",
            "const A = class {}; const B = class { /* Retain */ };",
        ),
    ],
)
def test_typescript_blob_comparison_rejects_cross_function_and_class_moves(
    tmp_path: Path, old: str, current: str
) -> None:
    before = parse_blobs(tmp_path, {"core.ts": old})["core.ts"]
    after = parse_blobs(tmp_path, {"core.ts": current})["core.ts"]
    assert new_note_indexes(after, before) == frozenset({0})


@pytest.mark.parametrize("container", ["", "class C:\n"])
def test_trailing_body_comments_cannot_move_between_functions(
    tmp_path: Path, container: str
) -> None:
    old = "def left():\n    pass\n    # Keep\n\ndef right():\n    pass\n\n"
    new = old.replace("    # Keep\n", "") + "    # Keep\n\n"
    if container:
        old = container + "".join("    " + line for line in old.splitlines(keepends=True))
        new = container + "".join("    " + line for line in new.splitlines(keepends=True))
        old += "    marker = 1\n"
        new += "    marker = 1\n"
    assert codes(tmp_path, old, old.replace("pass", "value = 1")) == []
    assert codes(tmp_path, old, new) == ["free-comment"]


def test_a_trailing_nested_comment_stays_in_the_nearest_actual_body(tmp_path: Path) -> None:
    old = (
        "def outer():\n    def inner():\n        pass\n        # Keep\n\n    # Outer\n\n# Module\n"
    )
    new = old.replace("        # Keep\n", "") + "# Keep\n"
    assert codes(tmp_path, old, new) == ["free-comment"]


def test_tab_indented_tails_use_visual_body_columns(tmp_path: Path) -> None:
    old = (
        "class C:\n\tdef left(self):\n\t\tpass\n                # Keep\n\n"
        "\tdef right(self):\n\t\tpass\n\n"
    )
    new = old.replace("                # Keep\n", "") + "                # Keep\n"
    assert codes(tmp_path, old, new) == ["free-comment"]


def test_shallower_tail_comments_belong_to_parent_or_module() -> None:
    source = parse_python(
        "core.py", "class C:\n\tdef f(self):\n\t\tpass\n        # Class\n\n# Module\n"
    )
    assert source.notes[0].lexical_owner.endswith("/class:C[1/1]")
    assert source.notes[1].lexical_owner == "<module>"


@pytest.mark.parametrize("declaration", ["def f(\n): pass\n", "class C(\n): pass\n"])
def test_a_multiline_header_inline_suite_does_not_swallow_the_module_tail(
    declaration: str,
) -> None:
    source = parse_python("core.py", declaration + "# Module\n")
    assert source.notes[0].lexical_owner == "<module>"


def test_the_tail_scope_does_not_expand_typed_symbol_or_reference_spans() -> None:
    source = parse_python("core.py", "def f():\n    pass\n    # Keep\n\n")
    assert source.symbols["f"].end_line == 2
    assert source.notes[0].owner == "<module>"
    assert source.notes[0].lexical_owner.endswith("/function:f[1/1]")
