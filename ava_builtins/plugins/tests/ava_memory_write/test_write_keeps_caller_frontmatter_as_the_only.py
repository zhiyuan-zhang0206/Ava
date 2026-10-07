"""Ava memory write cases: write keeps caller frontmatter as the only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ava
from ava_builtins.plugins.tests.test_ava_memory_write import (
    _ISO_SECONDS_RE,
    _frontmatter,
    _pool_validator,
    _pool_with_pointers,
)
from ava_builtins.plugins.tests.test_ava_memory_write import (
    memory_plugin as memory_plugin,
)


def test_write_keeps_caller_frontmatter_as_the_only_block(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """Content carrying its own block keeps it, completed — never a second
    block (the defect: the validator read the stub block while the caller's
    real block sat in the body)."""
    pool = _pool_with_pointers(tmp_path)
    content = (
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: Caller title\n"
        "description: Caller description\n"
        "tags: [type/project, demo]\n"
        "---\n"
        "<!-- agent-17 @ memory-host, 2026-09-10 13:00 -->\n"
        "\n"
        "Body.\n"
    )

    entry = ava.memory.write(
        "projects/demo/gamma-note",
        content,
        title="Arg title",
        description="Arg description",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    parts = written.split("---\n", 2)
    assert len(parts) == 3
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "Caller title"
    assert frontmatter["description"] == "Caller description"
    assert frontmatter["tags"] == ["type/project", "demo"]
    assert frontmatter["ava_machine"] == "memory-host"
    timestamp = frontmatter["timestamp"]
    assert isinstance(timestamp, str)
    assert _ISO_SECONDS_RE.fullmatch(timestamp)
    assert written.count("type: Memory") == 1
    assert parts[2] == "<!-- agent-17 @ memory-host, 2026-09-10 13:00 -->\n\nBody.\n"
    index = (pool / "MEMORY.md").read_text(encoding="utf-8")
    assert "gamma-note" not in index


def test_shared_write_completes_partial_frontmatter(memory_plugin: Any, tmp_path: Path) -> None:
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/delta-note",
        "---\ntitle: Partial title\n---\nBody.\n",
        store="shared",
        tags=["type/reference"],
    )

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "Partial title"
    assert frontmatter["type"] == "Memory"
    assert frontmatter["ava_agent"] == 17
    assert frontmatter["description"] == "Partial title"
    assert frontmatter["tags"] == ["type/reference"]
    assert frontmatter["ava_machine"] == "memory-host"
    timestamp = frontmatter["timestamp"]
    assert isinstance(timestamp, str)
    assert _ISO_SECONDS_RE.fullmatch(timestamp)


def test_shared_write_normalizes_fractional_timestamp(memory_plugin: Any, tmp_path: Path) -> None:
    """A caller block with microsecond precision is normalized: the pool
    format is second precision and the validator rejects the longer stamp."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/epsilon-note",
        "---\ntype: Memory\nava_agent: 17\ntitle: Epsilon\n"
        "description: Epsilon description\ntags: [type/reference]\n"
        "timestamp: '2026-09-10T05:15:02.406986+00:00'\n---\nBody.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    assert "timestamp: '2026-09-10T05:15:02+00:00'" in written
    assert ".406986" not in written


def test_personal_write_keeps_complete_frontmatter_byte_for_byte(
    memory_plugin: Any, tmp_path: Path
) -> None:
    content = (
        "---\nname: pers-note\ndescription: Caller description\ntags: [type/feedback]\n---\nBody.\n"
    )

    entry = ava.memory.write("pers-note", content)

    assert entry.read_text(encoding="utf-8") == content


def test_personal_write_completes_partial_frontmatter(memory_plugin: Any) -> None:
    entry = ava.memory.write("partial-note", "---\ntags: [type/feedback]\n---\nBody.\n")

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["name"] == "partial-note"
    assert frontmatter["description"] == "partial-note"
    assert frontmatter["tags"] == ["type/feedback"]


def test_write_quotes_values_yaml_would_misread(memory_plugin: Any, tmp_path: Path) -> None:
    """` #` starts a YAML comment mid-value and `: ` opens a mapping inside
    it — the block must read back every value exactly as given."""
    _pool_with_pointers(tmp_path)
    entry = ava.memory.write(
        "projects/demo/tricky-note",
        "Body.\n",
        title="Release #42: notes",
        description="Trap #1: quoted",
        store="shared",
    )
    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "Release #42: notes"
    assert frontmatter["description"] == "Trap #1: quoted"

    plain = ava.memory.write(
        "projects/demo/plain-note",
        "Body.\n",
        title="Plain title",
        description="Plain description",
        store="shared",
    )
    assert "title: Plain title\ndescription: Plain description\n" in plain.read_text(
        encoding="utf-8"
    )


def test_write_rejects_block_that_is_not_a_mapping(memory_plugin: Any) -> None:
    with pytest.raises(ValueError, match="not a non-empty"):
        ava.memory.write("bad-block-note", "---\njust prose\n---\nBody.\n")


def test_written_notes_pass_the_pool_validator(memory_plugin: Any, tmp_path: Path) -> None:
    """Every output shape passes the pool's own per-file validator — the gate
    the stub defect tripped on the shared store."""
    _pool_with_pointers(tmp_path)
    validate = _pool_validator()

    entries = [
        ava.memory.write("projects/demo/valid-plain", "Body.\n", store="shared"),
        ava.memory.write(
            "projects/demo/valid-args",
            "Body.\n",
            store="shared",
            title="With args",
            description="Described by an argument",
        ),
        ava.memory.write(
            "projects/demo/valid-caller",
            "---\ntype: Memory\nava_agent: 17\ntitle: Caller\n---\nBody.\n",
            store="shared",
        ),
        ava.memory.write(
            "projects/demo/valid-tricky",
            "Body.\n",
            store="shared",
            title="Tricky #1: value",
            description="Also # tricky: yes",
        ),
    ]
    for entry in entries:
        assert validate.validate_file(entry) == [], entry


def test_write_quotes_values_yaml_would_retype(memory_plugin: Any, tmp_path: Path) -> None:
    """Bare `null`, booleans, numbers and dates parse back as another type —
    a written `description: null` reads as empty and trips the pool gate."""
    _pool_with_pointers(tmp_path)

    for index, value in enumerate(["null", "123", "true", "2026-09-10"]):
        entry = ava.memory.write(
            f"projects/demo/retyped-{index}",
            "Body.\n",
            title=value,
            description=value,
            store="shared",
        )
        frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
        assert frontmatter["title"] == value
        assert frontmatter["description"] == value


def test_write_quotes_trailing_colon_values(memory_plugin: Any, tmp_path: Path) -> None:
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/colon-note",
        "Body.\n",
        title="Release notes:",
        description="Steps:",
        store="shared",
    )

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "Release notes:"
    assert frontmatter["description"] == "Steps:"


def test_write_quotes_tags_yaml_would_retype(memory_plugin: Any, tmp_path: Path) -> None:
    """A numeric tag parses back as an int, which the pool's validator
    rejects — it requires every tag to be a string."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/tag-note",
        "Body.\n",
        tags=["type/reference", "123"],
        store="shared",
    )

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["tags"] == ["type/reference", "123"]


def test_write_replaces_blank_fields_in_caller_block(memory_plugin: Any, tmp_path: Path) -> None:
    """A blank caller field is completed in place — the same key must not
    appear on two lines of the block."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/blank-note",
        '---\ntitle:\ndescription: ""\ntags: [type/project]\n---\nBody.\n',
        store="shared",
        title="Filled title",
    )

    written = entry.read_text(encoding="utf-8")
    block = written.split("---\n", 2)[1]
    assert block.count("title:") == 1
    assert block.count("description:") == 1
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "Filled title"
    assert frontmatter["description"] == "Filled title"


def test_write_quotes_caller_values_yaml_would_misread(memory_plugin: Any, tmp_path: Path) -> None:
    """A caller block's bare values get the same quoting as generated ones:
    an unquoted ` #` truncates the text at a comment, so the pool read would
    show less than the caller wrote."""
    pool = _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-hash-note",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: Release #42 notes\n"
        "description: Task #770 pylint baseline\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "Release #42 notes"
    assert frontmatter["description"] == "Task #770 pylint baseline"
    block = written.split("---\n", 2)[1]
    assert 'title: "Release #42 notes"' in block
    index = (pool / "MEMORY.md").read_text(encoding="utf-8")
    assert "caller-hash-note" not in index


def test_write_quotes_caller_values_the_block_read_would_reject(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """A trailing colon or a `: ` inside a bare value makes the whole block
    unreadable YAML; the value is quoted, so the write lands with the text
    the caller wrote instead of raising."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-colon-note",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: Release notes:\n"
        'description: Steps: "build" then ship\n'
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "Release notes:"
    assert frontmatter["description"] == 'Steps: "build" then ship'
    block = written.split("---\n", 2)[1]
    assert 'description: "Steps: \\"build\\" then ship"' in block


def test_write_quotes_caller_values_yaml_would_retype(memory_plugin: Any, tmp_path: Path) -> None:
    """Bare numbers and dates parse back as another type; a caller's `123`
    title and `17` agent id stay the text they wrote."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-retyped-note",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: 123\n"
        "description: 2026-09-10\n"
        "tags: [type/reference]\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "123"
    assert frontmatter["description"] == "2026-09-10"
    assert frontmatter["ava_agent"] == "17"


def test_write_keeps_caller_flow_collections_and_quotes_lookalikes(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """A complete flow collection is the caller's own YAML and stays one
    (`tags` must read back as a list); a value that only opens like one is
    text, so it is quoted. Line order and untouched lines are preserved."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-collection-note",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: [WIP] release notes\n"
        "description: {draft} notes\n"
        "tags: [type/project, demo]\n"
        'authors: ["#2481", "#1609"]\n'
        "timestamp: '2026-09-10T05:15:02+00:00'\n"
        "ava_machine: memory-host\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "[WIP] release notes"
    assert frontmatter["description"] == "{draft} notes"
    assert frontmatter["tags"] == ["type/project", "demo"]
    assert frontmatter["authors"] == ["#2481", "#1609"]
    block = written.split("---\n", 2)[1]
    assert block == (
        'type: Memory\nava_agent: "17"\ntitle: "[WIP] release notes"\n'
        'description: "{draft} notes"\ntags: [type/project, demo]\n'
        'authors: ["#2481", "#1609"]\ntimestamp: \'2026-09-10T05:15:02+00:00\'\n'
        "ava_machine: memory-host\n"
    )


def test_write_caller_quoting_is_idempotent(memory_plugin: Any, tmp_path: Path) -> None:
    """A quoted value reads back as written, so writing the note's own text
    again changes no byte of it."""
    _pool_with_pointers(tmp_path)
    content = (
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: Release #42 notes\n"
        "description: Steps: build then ship\n"
        "tags: [type/project]\n"
        "---\n"
        "Body.\n"
    )

    entry = ava.memory.write("projects/demo/caller-idempotent", content, store="shared")
    first = entry.read_text(encoding="utf-8")

    entry = ava.memory.write("projects/demo/caller-idempotent", first, store="shared")

    assert entry.read_text(encoding="utf-8") == first


def test_write_leaves_caller_quoting_in_place(memory_plugin: Any, tmp_path: Path) -> None:
    """A value the caller already quoted - single or double - is left exactly
    as written and still reads back as its content."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-quoted-note",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        'title: "Release #42 notes"\n'
        "description: 'Trap #1: quoted'\n"
        "tags: [type/project]\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    block = written.split("---\n", 2)[1]
    assert 'title: "Release #42 notes"' in block
    assert "description: 'Trap #1: quoted'" in block
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "Release #42 notes"
    assert frontmatter["description"] == "Trap #1: quoted"


def test_write_leaves_caller_block_sequences_alone(memory_plugin: Any, tmp_path: Path) -> None:
    """A block sequence under a key is YAML structure on its own lines - the
    line pass may not quote the entries or reorder them."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-block-sequence",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: Sequence note\n"
        "description: Sequence description\n"
        "tags:\n"
        "  - type/project\n"
        "  - demo\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    assert "  - type/project\n  - demo\n" in written
    frontmatter = _frontmatter(written)
    assert frontmatter["tags"] == ["type/project", "demo"]


def test_write_leaves_caller_multiline_quoted_scalars_alone(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """A quote that does not close on its line opens a multi-line scalar -
    quoting it shut would break a block the block read still accepts."""
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write(
        "projects/demo/caller-multiline-quote",
        "---\n"
        "type: Memory\n"
        "ava_agent: 17\n"
        "title: 'single\n"
        "  line'\n"
        'description: "double\n'
        '  line"\n'
        "tags: [type/project]\n"
        "---\n"
        "Body.\n",
        store="shared",
    )

    written = entry.read_text(encoding="utf-8")
    assert "title: 'single\n  line'\n" in written
    assert 'description: "double\n  line"\n' in written
    frontmatter = _frontmatter(written)
    assert frontmatter["title"] == "single line"
    assert frontmatter["description"] == "double line"


def test_write_terminates_entry_with_newline(memory_plugin: Any, tmp_path: Path) -> None:
    """`write` publishes each entry with a trailing newline: a body handed in
    without one gains a terminal newline, a body that already closes with one
    stays untouched (idempotent), and the caller-frontmatter and shared paths
    land the same way."""
    entry = ava.memory.write(
        "terminal-newline",
        "Body without a trailing newline.",
        title="Terminal newline",
        description="Entries end with a newline",
        tags=["type/feedback"],
    )
    assert entry.read_text(encoding="utf-8").endswith("Body without a trailing newline.\n")

    ava.memory.write(
        "terminal-newline",
        "Body that already ends with a newline.\n",
        title="Terminal newline",
        description="Entries end with a newline",
        tags=["type/feedback"],
    )
    text = entry.read_text(encoding="utf-8")
    assert text.endswith("Body that already ends with a newline.\n")
    assert not text.endswith("\n\n")

    entry = ava.memory.write(
        "terminal-newline-caller-block",
        "---\n"
        "name: terminal-newline-caller-block\n"
        "description: caller block without a terminal newline\n"
        "tags: [type/feedback]\n"
        "---\n"
        "Body after a caller block, no newline.",
        tags=["type/feedback"],
    )
    text = entry.read_text(encoding="utf-8")
    assert text.endswith("Body after a caller block, no newline.\n")

    _pool_with_pointers(tmp_path)
    entry = ava.memory.write(
        "projects/demo/terminal-newline",
        "Shared body without a trailing newline.",
        title="Terminal newline shared",
        description="Shared entries end with a newline",
        tags=["type/project"],
        store="shared",
    )
    text = entry.read_text(encoding="utf-8")
    assert text.endswith("Shared body without a trailing newline.\n")
