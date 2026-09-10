"""`ava.memory.write` store-owned paths, frontmatter, and index invariants."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module, util
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import ava
from agent.state import build_agent_state, clear_plugin_registrations
from shared.plugin_context import PluginContext


@pytest.fixture
def memory_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Load ava_memory through its registration path against isolated stores."""
    import ava._boot as boot
    import shared.machine
    import shared.paths

    workspace = tmp_path / "workspace"
    pool = tmp_path / "pool"

    def isolated_home() -> Path:
        return tmp_path

    def isolated_workspace(_agent_id: int) -> Path:
        return workspace

    def isolated_pool() -> Path:
        return pool

    def isolated_machine_name() -> str:
        return "memory-host"

    monkeypatch.setattr(shared.paths, "ava_home", isolated_home)
    monkeypatch.setattr(shared.paths, "workspace_dir", isolated_workspace)
    monkeypatch.setattr(shared.paths, "memory_dir", isolated_pool)
    monkeypatch.setattr(shared.machine, "machine_name", isolated_machine_name)
    monkeypatch.setattr(boot, "_agent_id", 17)

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]

    with PluginContext("ava_memory"):
        from ava_builtins.plugins.ava_memory import plugin as plugin

    yield plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]


def test_personal_write_creates_entry_and_upserts_index(memory_plugin: Any, tmp_path: Path) -> None:
    entry = ava.memory.write(
        "working-style",
        "Prefer small, verified changes.\n",
        title="Working style",
        description="How this agent approaches changes",
        tags=["type/feedback"],
    )

    expected = tmp_path / "workspace" / "memory" / "working-style.md"
    assert entry == expected.resolve()
    assert entry.read_text(encoding="utf-8") == (
        "---\n"
        "name: working-style\n"
        "description: How this agent approaches changes\n"
        "tags: [type/feedback]\n"
        "---\n\n"
        "Prefer small, verified changes.\n"
    )
    index = expected.parent / "MEMORY.md"
    assert index.read_text(encoding="utf-8") == (
        "- [Working style](working-style.md) — How this agent approaches changes\n"
    )

    ava.memory.write(
        "working-style",
        "Prefer narrow changes.\n",
        title="Working style",
        description="Revised working preference",
        tags=["type/feedback"],
    )

    assert index.read_text(encoding="utf-8") == (
        "- [Working style](working-style.md) — Revised working preference\n"
    )


def test_personal_write_uses_hosted_turn_identity(
    memory_plugin: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shared.paths
    from shared.turn_identity import bind_turn_identity

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    monkeypatch.setattr(shared.paths, "workspace_dir", workspace)
    with bind_turn_identity(29):
        entry = ava.memory.write("hosted-note", "Belongs to agent 29.")

    assert entry == tmp_path / "29" / "memory" / "hosted-note.md"
    assert not (tmp_path / "17").exists()


def test_write_truncates_long_description_in_index_pointer(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """The index is injected into agent context, so an unbounded description
    would leak into the context budget: the MEMORY.md pointer truncates the
    description while the note keeps the full text (QA nit, #844)."""
    from ava_builtins.plugins.ava_memory.sdk import _INDEX_DESCRIPTION_MAX

    long_description = "d" * (_INDEX_DESCRIPTION_MAX + 50)
    ava.memory.write(
        "long-desc",
        "Body.\n",
        title="Long description",
        description=long_description,
        tags=["type/reference"],
    )

    entry = tmp_path / "workspace" / "memory" / "long-desc.md"
    assert f"description: {long_description}" in entry.read_text(encoding="utf-8")
    index = (tmp_path / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert index == (f"- [Long description](long-desc.md) — {'d' * _INDEX_DESCRIPTION_MAX}...\n")


def test_concurrent_personal_writes_preserve_all_index_pointers(
    memory_plugin: Any, tmp_path: Path
) -> None:
    writers = 10

    def write_entry(index: int) -> None:
        ava.memory.write(
            f"concurrent-note-{index}",
            f"Concurrent note {index}.\n",
            title=f"Concurrent note {index}",
            description=f"Concurrent description {index}",
        )

    with ThreadPoolExecutor(max_workers=writers) as executor:
        list(executor.map(write_entry, range(writers)))

    index = tmp_path / "workspace" / "memory" / "MEMORY.md"
    assert set(index.read_text(encoding="utf-8").splitlines()) == {
        f"- [Concurrent note {number}](concurrent-note-{number}.md) — "
        f"Concurrent description {number}"
        for number in range(writers)
    }


def test_shared_write_uses_pool_frontmatter_and_pointers_section(
    memory_plugin: Any, tmp_path: Path
) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "MEMORY.md").write_text(
        "# Shared memory\n\n## Pointers\n\n- [Existing](existing.md) — Existing note\n\n## Archive\n",
        encoding="utf-8",
    )

    entry = ava.memory.write(
        "health/user-health-overview",
        "The user tracks daily symptoms.\n",
        title="User health overview",
        description="Durable health context",
        tags=["type/project"],
        store="shared",
    )

    assert entry == (pool / "health" / "user-health-overview.md").resolve()
    written = entry.read_text(encoding="utf-8")
    assert "type: Memory\nava_agent: 17\ntitle: User health overview\n" in written
    assert "description: Durable health context\ntags: [type/project]\n" in written
    assert "timestamp: '" in written
    assert "ava_machine: memory-host\n" in written
    assert "<!-- agent-17 @ memory-host, " in written
    index = (pool / "MEMORY.md").read_text(encoding="utf-8")
    assert index.index("- [User health overview](health/user-health-overview.md)") < index.index(
        "## Archive"
    )


def test_personal_write_rejects_directory_slug(memory_plugin: Any) -> None:
    with pytest.raises(ValueError, match="kebab-case"):
        ava.memory.write("health/user-health-overview", "body")


def test_write_requires_exactly_one_type_tag(memory_plugin: Any) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ava.memory.write("tagged-note", "body", tags=["type/project", "type/reference"])


def test_personal_write_is_immune_to_ava_cwd_drift(memory_plugin: Any, tmp_path: Path) -> None:
    """The dedicated API derives its destination from the agent, not ava.cwd."""
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_code"):
            del sys.modules[name]
    with PluginContext("ava_code"):
        import_module("ava_builtins.plugins.ava_code.plugin")

    drifted_cwd = tmp_path / "repository"
    drifted_cwd.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_cls = build_agent_state()
    state_kwargs: dict[str, object] = {
        "ava_code__cwd": str(workspace),
        "ava_code__last_seen_compact": 0,
    }
    ava.state = state_cls(messages=[], halted=False, **state_kwargs)  # pyright: ignore[reportUnknownArgumentType, reportArgumentType]
    ava.state_update = {}
    try:
        ava.cwd.set(drifted_cwd)
        entry = ava.memory.write("cwd-proof", "memory body")
    finally:
        ava.state = None
        ava.state_update = None

    assert entry == (workspace / "memory" / "cwd-proof.md").resolve()
    assert not (drifted_cwd / "memory" / "cwd-proof.md").exists()


def test_plugin_loads_and_writes_without_fcntl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows smoke: plugin import + index update with no fcntl module.

    CI has no Windows runner — the root cause of 6e96b1554, where an
    unguarded top-level ``import fcntl`` crashed every Windows agent at
    plugin load.
    Simulate Windows's missing fcntl in-process by making ``import fcntl``
    raise ImportError while the plugin loads and writes. Trade-off vs a real
    Windows runner: only the fcntl absence is simulated, not msvcrt or other
    platform quirks — but the ImportError mechanism is exactly what broke
    Windows boot, and the write path is asserted to still work unguarded.
    """
    import builtins

    sys.modules.pop("fcntl", None)  # collection may have imported it; the fake must intercept
    real_import = builtins.__import__

    def no_fcntl(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = None,
        level: int = 0,
    ) -> Any:
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)

    import ava._boot as boot
    import shared.machine
    import shared.paths

    workspace = tmp_path / "workspace"
    pool = tmp_path / "pool"

    def isolated_home() -> Path:
        return tmp_path

    def isolated_workspace(_agent_id: int) -> Path:
        return workspace

    def isolated_pool() -> Path:
        return pool

    monkeypatch.setattr(shared.paths, "ava_home", isolated_home)
    monkeypatch.setattr(shared.paths, "workspace_dir", isolated_workspace)
    monkeypatch.setattr(shared.paths, "memory_dir", isolated_pool)
    monkeypatch.setattr(shared.machine, "machine_name", lambda: "memory-host")
    monkeypatch.setattr(boot, "_agent_id", 17)

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]

    try:
        with PluginContext("ava_memory"):
            from ava_builtins.plugins.ava_memory import plugin as plugin
            from ava_builtins.plugins.ava_memory import sdk as memory_sdk

        assert memory_sdk.fcntl is None  # the guard fired: fcntl unavailable

        entry = ava.memory.write(
            "no-fcntl",
            "Body written without fcntl.\n",
            title="No fcntl",
            description="Windows smoke write",
            tags=["type/reference"],
        )
        index = entry.parent / "MEMORY.md"
        assert index.read_text(encoding="utf-8") == (
            "- [No fcntl](no-fcntl.md) — Windows smoke write\n"
        )
    finally:
        clear_plugin_registrations()
        for name in list(sys.modules):
            if name.startswith("ava_builtins.plugins.ava_memory"):
                del sys.modules[name]


def test_memory_namespace_importable_as_submodule(memory_plugin: Any) -> None:
    """`import ava.memory` resolves once the plugin registers the namespace —
    the LLM habit the import fix targets (agent bug: a bare `import ava.memory`
    raised ModuleNotFoundError while `ava.memory.write` attribute access
    worked). The import must serve the same object the package attribute
    holds, with the members reachable either way."""
    import types
    from importlib import import_module

    mod = import_module("ava.memory")

    assert isinstance(mod, types.ModuleType)
    assert mod is ava.memory
    assert mod.PATH is ava.memory.PATH
    assert mod.IndexerUnavailable is ava.memory.IndexerUnavailable
    assert callable(mod.write)
    assert callable(mod.search)


_ISO_SECONDS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})")


def _pool_validator() -> Any:
    """The pool's per-file validator, as shipped with the plugin template.

    Loading the shipped artifact keeps the assertion on the gate the pool
    actually runs, instead of a paraphrase that could pass while it fails.
    """
    path = (
        Path(__file__).resolve().parents[2] / "ava_builtins/plugins/ava_memory/template/validate.py"
    )
    spec = util.spec_from_file_location("_pool_validate", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(text: str) -> dict[str, Any]:
    """First frontmatter block, parsed the way the pool validator reads it."""
    assert text.startswith("---\n")
    parts = text.split("---\n", 2)
    assert len(parts) >= 3
    parsed = yaml.safe_load(parts[1])
    assert isinstance(parsed, dict)
    return cast("dict[str, Any]", parsed)


def _pool_with_pointers(tmp_path: Path) -> Path:
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "MEMORY.md").write_text("# Shared\n\n## Pointers\n", encoding="utf-8")
    return pool


def test_shared_write_defaults_title_and_description_to_file_name(
    memory_plugin: Any, tmp_path: Path
) -> None:
    """A blank description is not an option (the field is required), and the
    title defaults to the file name — not the topic path the stub defect
    wrote into every reader's index."""
    pool = _pool_with_pointers(tmp_path)

    entry = ava.memory.write("projects/demo/alpha-note", "Body.\n", store="shared")

    frontmatter = _frontmatter(entry.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "alpha-note"
    assert frontmatter["description"] == "alpha-note"
    index = (pool / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [alpha-note](projects/demo/alpha-note.md) — alpha-note" in index


def test_shared_write_timestamp_is_second_precision(memory_plugin: Any, tmp_path: Path) -> None:
    _pool_with_pointers(tmp_path)

    entry = ava.memory.write("projects/demo/beta-note", "Body.\n", store="shared")

    timestamp = _frontmatter(entry.read_text(encoding="utf-8"))["timestamp"]
    assert isinstance(timestamp, str)
    assert _ISO_SECONDS_RE.fullmatch(timestamp)


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
    assert "- [Caller title](projects/demo/gamma-note.md) — Caller description" in index


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
    assert (
        "- [Release #42 notes](projects/demo/caller-hash-note.md) — Task #770 pylint baseline"
        in index
    )


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
