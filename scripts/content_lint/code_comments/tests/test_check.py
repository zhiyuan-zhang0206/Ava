from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import Repository, check
from scripts.content_lint.code_comments.model import NoteKind
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.tests.cases import DOC, files, note, repository


def codes(repo: Repository) -> list[str]:
    return [item.code for item in check(repo, strict_paths=repo.files).findings]


def test_note_kinds_are_the_closed_migration_vocabulary() -> None:
    assert [kind.value for kind in NoteKind] == [
        "comment",
        "note",
        "docstring",
        "unused-string",
        "lint-directive",
        "machine",
        "fixture-help",
    ]


def test_a_note_block_spans_lines_and_keeps_paragraphs() -> None:
    source = parse_python(
        "core.py",
        note(
            (
                "The gateway re-resolves the principal on every refresh,",
                "so a cached token cannot outlive a role change.",
                "",
                "Refresh after the role change lands.",
            ),
            links=(DOC,),
        )
        + "def refresh() -> None: ...\n",
    )
    (entry,) = source.notes
    assert entry.kind is NoteKind.NOTE
    assert (entry.line, entry.end_line) == (1, 6)
    assert entry.text == (
        "The gateway re-resolves the principal on every refresh,\n"
        "so a cached token cannot outlive a role change.\n"
        "\n"
        "Refresh after the role change lands."
    )
    assert entry.links == (DOC,)
    assert entry.owner == "refresh"


def test_a_link_only_block_points_at_shared_documentation(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=(DOC, "pkg/docs/other.md")) + (
        "def budget() -> int:\n    return 1\n"
    )
    source["pkg/docs/other.md"] = "# Other\n"
    repo = repository(tmp_path, source)
    result = check(repo, strict_paths=repo.files)
    assert result.findings == []
    entry = repo.source("pkg/core.py").notes[0]
    assert entry.kind is NoteKind.NOTE
    assert (entry.line, entry.end_line, entry.text) == (1, 3, "")
    assert entry.links == (DOC, "pkg/docs/other.md")


@pytest.mark.parametrize("block", [note(), note(("",)), note(("", ""))])
def test_a_note_needs_text_or_at_least_one_link(tmp_path: Path, block: str) -> None:
    source = files()
    source["pkg/core.py"] = block + "def budget() -> int:\n    return 1\n"
    assert codes(repository(tmp_path, source)) == ["empty-note"]


def test_body_lines_after_the_links_are_new_prose(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = (
        note(("Stated here.",), links=(DOC,))
        + "# trailing prose\n"
        + "def budget() -> int:\n    return 1\n"
    )
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert [(item.code, item.line) for item in findings] == [("free-comment", 4)]
    entry = repo.source("pkg/core.py").notes[0]
    assert (entry.end_line, entry.text, entry.links) == (3, "Stated here.", (DOC,))


def test_a_body_continues_only_at_the_opening_column() -> None:
    source = parse_python("core.py", "# meiva:note\n# Body\n  # Deeper\n")
    assert [(entry.kind, entry.line, entry.text) for entry in source.notes] == [
        (NoteKind.NOTE, 1, "Body"),
        (NoteKind.COMMENT, 3, "Deeper"),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "# meiva:note because it matters",
        "## meiva:note",
        "#meiva:note",
        "# meiva:notes",
        "# meiva:note ",
    ],
)
def test_the_note_header_accepts_no_inline_text(tmp_path: Path, header: str) -> None:
    source = files()
    source["pkg/core.py"] = header + "\ndef budget() -> int:\n    return 1\n"
    assert codes(repository(tmp_path, source)) == ["free-comment"]


@pytest.mark.parametrize(
    "link",
    [
        "pkg/docs/missing.md",
        "pkg/docs/core.md#budget",
        "../pkg/docs/core.md",
        "/pkg/docs/core.md",
        "pkg/docs",
        "pkg\\docs\\core.md",
    ],
)
def test_a_link_is_an_exact_repo_relative_tracked_path(tmp_path: Path, link: str) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=(link,)) + "def budget() -> int:\n    return 1\n"
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert [(item.code, item.line) for item in findings] == [("note-link", 1)]


def test_a_document_outside_the_tracked_universe_cannot_be_linked(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=("pkg/docs/new.md",)) + (
        "def budget() -> int:\n    return 1\n"
    )
    repo = repository(tmp_path, source)
    (tmp_path / "pkg/docs/new.md").write_text("# New\n", encoding="utf-8")
    assert codes(repo) == ["note-link"]


def test_a_linked_symlink_is_not_a_regular_repository_file(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=(DOC,)) + "def budget() -> int:\n    return 1\n"
    source["pkg/docs/real.md"] = "# Real\n"
    repo = repository(tmp_path, source)
    (tmp_path / DOC).unlink()
    (tmp_path / DOC).symlink_to("real.md")
    findings = check(repo, strict_paths=repo.files).findings
    assert [item.code for item in findings] == ["note-link"]
    assert "not a regular in-repository file" in findings[0].message


def test_links_are_validated_outside_the_strict_scope(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=("pkg/docs/missing.md",)) + (
        "def budget() -> int:\n    return 1\n"
    )
    assert [item.code for item in check(repository(tmp_path, source)).findings] == ["note-link"]


@pytest.mark.parametrize(
    ("declaration", "owner"),
    [
        ("def refresh() -> None: ...\n", "refresh"),
        ("class Worker:\n    pass\n", "Worker"),
        ("@decorator\ndef refresh() -> None: ...\n", "refresh"),
    ],
)
def test_a_standalone_block_binds_the_following_declaration(declaration: str, owner: str) -> None:
    source = parse_python("core.py", note(("Stated here.",), links=(DOC,)) + declaration)
    assert source.notes[0].owner == owner


def test_a_block_separated_from_its_declaration_binds_the_module() -> None:
    source = parse_python(
        "core.py", note(("Module fact.",), links=(DOC,)) + "\ndef refresh() -> None: ...\n"
    )
    assert source.notes[0].owner == "<module>"


@pytest.mark.parametrize(
    ("text", "owner"),
    [
        (
            "class Worker:\n"
            + note(("Stated here.",), indent=4)
            + "    def refresh(self) -> None:\n        return None\n",
            "Worker",
        ),
        (
            "def outer() -> None:\n"
            + note(("Stated here.",), indent=4)
            + "    def inner() -> None:\n        return None\n",
            "outer",
        ),
        (
            "def outer() -> None:\n    def inner() -> None:\n"
            + note(("Stated here.",), indent=8)
            + "        return None\n",
            "outer.inner",
        ),
    ],
)
def test_a_block_inside_a_declaration_binds_the_innermost_definition(text: str, owner: str) -> None:
    assert parse_python("core.py", text).notes[0].owner == owner


@pytest.mark.parametrize(
    ("extra", "kind"),
    [
        ("# Section banner\n", "comment"),
        ('"""Implementation note."""\n', "unused-string"),
        ('value = 1\n"""Attribute help."""\n', "docstring"),
        ("# meiva:ref pkg/docs/core.md\n", "comment"),
    ],
)
def test_internal_prose_fails_the_strict_check(tmp_path: Path, extra: str, kind: str) -> None:
    source = files()
    source["pkg/core.py"] += extra
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert [item.code for item in findings] == ["free-comment"]
    assert findings[0].message.startswith(f"{kind}: ")


def test_a_function_docstring_is_internal_prose(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note(("Stated here.",), links=(DOC,)) + (
        'def budget() -> int:\n    """Explains the body."""\n    return 1\n'
    )
    assert codes(repository(tmp_path, source)) == ["free-comment"]


@pytest.mark.parametrize(
    "marker",
    [
        '__all_for_meiva__ = ["budget"]',
        'Surface = SdkNamespace("meiva")',
        'Surface = SdkMember("read")',
    ],
)
def test_agent_surface_docstrings_are_native_help(tmp_path: Path, marker: str) -> None:
    source = files()
    source["pkg/core.py"] = note((), links=(DOC,)) + (
        'def budget() -> int:\n    """What callers read through help()."""\n    return 1\n'
    )
    source["pkg/core.py"] += marker + "\n"
    assert codes(repository(tmp_path, source)) == []


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/usr/bin/env python",
        "#!/usr/bin/env python3",
        "#!/usr/bin/env python3.12",
        "#!/usr/bin/python3",
        "#!/usr/bin/python3.12",
    ],
)
def test_a_python_entry_shebang_is_syntax(tmp_path: Path, shebang: str) -> None:
    source = files()
    source["pkg/core.py"] = shebang + "\n" + source["pkg/core.py"]
    assert codes(repository(tmp_path, source)) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "#!/usr/bin/env python because it is needed\n",
        "#!/usr/bin/env python # a reason\n",
        "#!/usr/bin/env python -u\n",
        "#!/usr/bin/env -S python\n",
        "#!/usr/bin/env python2\n",
        "#!/usr/bin/env python3.13\n",
        "#!/usr/bin/python\n",
        "#!/usr/local/bin/python\n",
        "#!/usr/bin/env pythons\n",
        "# !/usr/bin/env python\n",
        " #!/usr/bin/env python\n",
        "\n#!/usr/bin/env python\n",
    ],
)
def test_a_python_shebang_does_not_hide_comments(tmp_path: Path, prefix: str) -> None:
    source = files()
    source["pkg/core.py"] = prefix + source["pkg/core.py"]
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert [(item.path, item.code) for item in findings] == [("pkg/core.py", "free-comment")]


def test_comment_shaped_strings_are_values_not_comments(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] += "message = '# a value, not a comment'\n"
    assert codes(repository(tmp_path, source)) == []


def test_a_broken_source_is_reported_in_the_strict_scope(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"pkg/core.py": "def broken(\n"})
    assert [item.code for item in check(repo, strict_paths=repo.files).findings] == ["source-parse"]


def test_a_symlinked_source_cannot_be_checked(tmp_path: Path) -> None:
    repo = repository(tmp_path, files())
    (tmp_path / "pkg/core.py").unlink()
    (tmp_path / "pkg/core.py").symlink_to(tmp_path / "pkg/tests/test_core.py")
    findings = check(repo, strict_paths=repo.files).findings
    assert [item.code for item in findings] == ["source-parse"]
    assert "not a regular in-repository file" in findings[0].message


def test_the_result_counts_notes_and_parsed_sources(tmp_path: Path) -> None:
    source = files()
    source["pkg/legacy.py"] = "# Unmigrated source.\n"
    repo = repository(tmp_path, source)
    loose = check(repo)
    assert (loose.notes, loose.checked_sources, loose.findings) == (1, 1, [])
    strict = check(repo, strict_paths=repo.files)
    assert (strict.notes, strict.checked_sources) == (1, 3)
    assert [(item.path, item.code) for item in strict.findings] == [
        ("pkg/legacy.py", "free-comment")
    ]


def test_links_only_validation_does_not_claim_completed_migration(tmp_path: Path) -> None:
    source = files()
    source["pkg/legacy.py"] = "# Unmigrated source.\n"
    repo = repository(tmp_path, source)
    assert check(repo).findings == []
    assert any(
        item.path == "pkg/legacy.py" and item.code == "free-comment"
        for item in check(repo, strict_paths=repo.files).findings
    )
