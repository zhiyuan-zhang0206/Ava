from pathlib import Path

import pytest

from scripts.content_lint.code_comments.changes import new_note_indexes
from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.tests.cases import repository

BASE = '# Module note\nimport math\n\ndef budget():\n    """Budget note."""\n    value = 1\n    # Keep a reserve\n    return value\n'


def findings(tmp_path: Path, previous: str, current: str) -> list[str]:
    repo = repository(tmp_path, {"core.py": current})
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
    assert findings(tmp_path, BASE, current) == []


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
    assert "free-comment" in findings(tmp_path, BASE, current)


def test_same_owner_reordering_consumes_each_old_occurrence_once(tmp_path: Path) -> None:
    old = "def f():\n    # First note\n    # Second note\n    return 1\n"
    current = old.replace("# First note\n    # Second note", "# Second note\n    # First note")
    assert findings(tmp_path, old, current) == []


@pytest.mark.parametrize("keyword", ["def", "class"])
def test_cross_owner_moves_do_not_share_old_note_credit(tmp_path: Path, keyword: str) -> None:
    old = f"{keyword} left{'()' if keyword == 'def' else ''}:\n    # Shared text\n    pass\n{keyword} right{'()' if keyword == 'def' else ''}:\n    pass\n"
    current = old.replace("    # Shared text\n", "").rstrip() + "\n    # Shared text\n"
    assert findings(tmp_path, old, current) == ["free-comment"]


DUPLICATE = "if flag:\n    def f():\n        # First note\n        return 1\nelse:\n    def f():\n        # Second note\n        return 2\n"


def test_duplicate_declarations_keep_separate_occurrence_credit(tmp_path: Path) -> None:
    assert findings(tmp_path, DUPLICATE, DUPLICATE.replace("return 1", "return 3")) == []
    moved = DUPLICATE.replace("        # First note\n", "").replace(
        "        # Second note", "        # First note\n        # Second note"
    )
    assert findings(tmp_path, DUPLICATE, moved) == ["free-comment"]


def test_duplicate_group_growth_cannot_reuse_an_inserted_ordinal(tmp_path: Path) -> None:
    old = "def f():\n    # First note\n    return 1\n\ndef f():\n    return 2\n"
    new = "def f():\n    # First note\n    return 0\n\n" + old.replace("    # First note\n", "")
    assert findings(tmp_path, old, new) == ["free-comment"]


def test_function_class_collision_and_nested_parents_keep_distinct_identity(tmp_path: Path) -> None:
    old = "def x():\n    # First note\n    pass\nclass x:\n    pass\n"
    new = old.replace("    # First note\n", "") + "    # First note\n"
    assert findings(tmp_path, old, new) == ["free-comment"]
    nested = "class C:\n    def f(self):\n        # First note\n        pass\nclass C:\n    def f(self):\n        pass\n"
    assert findings(
        tmp_path, nested, nested.replace("        # First note\n", "") + "        # First note\n"
    ) == ["free-comment"]


def test_property_getter_setter_code_change_does_not_migrate_other_notes(tmp_path: Path) -> None:
    old = "class Item:\n    @property\n    def value(self):\n        # Getter note\n        return 1\n    @value.setter\n    def value(self, value):\n        # Setter note\n        self._value = value\n"
    assert findings(tmp_path, old, old.replace("return 1", "return 2")) == []


def test_old_directives_do_not_need_new_rationale_but_new_ones_do(tmp_path: Path) -> None:
    old = "def f():\n    return call(1)  # noqa: S603\n"
    assert findings(tmp_path, old, old.replace("call(1)", "call(2)")) == []
    assert findings(tmp_path, old, old.replace("S603", "S603, S607")) == ["directive-owner"]


def test_inline_module_comment_does_not_borrow_adjacent_function_identity(tmp_path: Path) -> None:
    old = "import math  # Legacy import\ndef f():\n    return 1\n"
    assert findings(tmp_path, old, old.replace("def f", "def g")) == []


def test_multiline_crlf_notes_use_exact_blob_text(tmp_path: Path) -> None:
    old = 'def f():\r\n    """First line.\r\n    Second line.\r\n    """\r\n    return 1\r\n'
    assert findings(tmp_path, old, old.replace("return 1", "return 2")) == []
    assert findings(tmp_path, old, old.replace("Second", "Changed")) == ["free-comment"]


@pytest.mark.parametrize("broken", ["record", "test", "malformed"])
def test_global_reference_integrity_survives_legacy_matching(tmp_path: Path, broken: str) -> None:
    from scripts.content_lint.code_comments.tests.cases import files

    contents = files()
    if broken == "record":
        contents.pop("pkg/docs/core.meiva.okf.md")
    elif broken == "test":
        contents["pkg/tests/test_core.py"] = "def removed_test(): pass\n"
    else:
        contents["pkg/core.py"] = contents["pkg/core.py"].replace(
            "# meiva:ref", "# malformed meiva:ref"
        )
    repo = repository(tmp_path, contents)
    path = "pkg/core.py"
    source = parse_python(path, contents[path])
    notes = {path: new_note_indexes(source, source)}
    assert notes == {path: frozenset()}
    assert check(repo, strict_paths=frozenset(notes), strict_notes=notes).findings


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
    from scripts.content_lint.code_comments.changes import parse_blobs

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
    assert findings(tmp_path, old, old.replace("pass", "value = 1")) == []
    assert findings(tmp_path, old, new) == ["free-comment"]


def test_trailing_nested_comment_stays_in_nearest_actual_body(tmp_path: Path) -> None:
    old = (
        "def outer():\n    def inner():\n        pass\n        # Keep\n\n    # Outer\n\n# Module\n"
    )
    new = old.replace("        # Keep\n", "") + "# Keep\n"
    assert findings(tmp_path, old, new) == ["free-comment"]


def test_tab_indented_tail_uses_visual_body_columns(tmp_path: Path) -> None:
    old = "class C:\n\tdef left(self):\n\t\tpass\n                # Keep\n\n\tdef right(self):\n\t\tpass\n\n"
    new = old.replace("                # Keep\n", "") + "                # Keep\n"
    assert findings(tmp_path, old, new) == ["free-comment"]


def test_shallower_tail_comments_belong_to_parent_or_module() -> None:
    source = parse_python(
        "core.py", "class C:\n\tdef f(self):\n\t\tpass\n        # Class\n\n# Module\n"
    )
    assert source.notes[0].lexical_owner.endswith("/class:C[1/1]")
    assert source.notes[1].lexical_owner == "<module>"


@pytest.mark.parametrize("declaration", ["def f(\n): pass\n", "class C(\n): pass\n"])
def test_multiline_header_inline_suite_does_not_swallow_module_tail(declaration: str) -> None:
    source = parse_python("core.py", declaration + "# Module\n")
    assert source.notes[0].lexical_owner == "<module>"


def test_tail_scope_does_not_expand_typed_symbol_or_reference_spans() -> None:
    source = parse_python("core.py", "def f():\n    pass\n    # Keep\n\n")
    assert source.symbols["f"].end_line == 2
    assert source.notes[0].owner == "<module>"
    assert source.notes[0].lexical_owner.endswith("/function:f[1/1]")
