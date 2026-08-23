"""`ava.memory.write` store-owned paths, frontmatter, and index invariants."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

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
