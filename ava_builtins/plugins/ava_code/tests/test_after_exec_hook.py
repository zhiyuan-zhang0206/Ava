"""`plugins.ava_code` after_exec hook — the notes `ava.cwd.set` left pending reach the agent.

The hook runs in the agent host, where no exec slot is bound: it reads the graph state it is
handed. These tests hand it a real state class instance (the same one an exec's committed
`ava.cwd.set` produces), so a hook that reads through the exec slot instead fails here.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import ava
from agent.state import BaseAgentState, CompactState, build_agent_state
from ava.sdk_surface import install
from ava_builtins.plugins.ava_code.tests.surface_support import code_registry
from base.packages.plugins.extensions import ExtensionRegistry


@pytest.fixture(autouse=True)
def _installed_surface():
    install.install(code_registry())
    yield
    install.uninstall()
    ava.unbind_exec_turn()


def _make_state_with_cwd(cwd: str) -> BaseAgentState:
    from ava_builtins.plugins.ava_code import agent_runtime

    state_cls = build_agent_state(ExtensionRegistry((("ava_code", agent_runtime.contribute()),)))
    fields: dict[str, Any] = {"ava_code__cwd": cwd}
    return state_cls(messages=[], halted=False, **fields)


def _hook_notes(result: dict | None) -> list[tuple[str, str]]:
    """(note tag, text) of every system note the hook returned in its messages delta."""
    from typing import cast

    from langchain_core.messages import AnyMessage

    from agent.messages import read_ava_kwargs

    assert result is not None
    return [
        (read_ava_kwargs(m).get("ava_note_tag", ""), str(m.content).removeprefix("[system] "))  # pyright: ignore[reportUnknownMemberType]
        for m in cast(list[AnyMessage], result["messages"])
    ]


def _repo_with_project_skill(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    demo = repo / ".ava" / "skills" / "demo-proj"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo-proj\ndescription: a project-local demo\n---\nbody",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


async def test_after_exec_hook_injects_cwd_and_project_skills_notes(tmp_path: Path):
    """After an exec's `ava.cwd.set(repo)`, the host-side hook consumes `cwd_note` (clears it) and
    injects the cwd-change note plus the project-skills listing."""
    from ava_builtins.plugins.ava_code.agent_runtime import inject_cwd_notes_after_exec

    repo = _repo_with_project_skill(tmp_path)
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        ava.cwd.set(repo)
        committed = ava.state  # the working copy is exactly what the exec commits to graph state
    finally:
        ava.unbind_exec_turn()  # the host holds no exec slot

    with pytest.raises(AttributeError):
        _ = ava.state
    result = await inject_cwd_notes_after_exec(committed, None, None)

    notes = _hook_notes(result)
    assert [tag for tag, _ in notes] == ["context", "project_skills"]
    assert notes[0][1] == f"Working directory set to {repo}"
    assert "demo-proj" in notes[1][1]
    assert result is not None
    assert result["ava_code__cwd_note"] is None
    assert result["ava_code__project_skills_seen_compact"] == 0


async def test_after_exec_hook_reinjects_project_skills_only_after_compact(tmp_path: Path):
    """The skills listing is injected once per compaction: a state that already saw this compact
    version gets nothing; a compaction bumps the version and re-surfaces it."""
    from ava_builtins.plugins.ava_code.agent_runtime import inject_cwd_notes_after_exec

    state_cls = type(_make_state_with_cwd(str(tmp_path)))
    note = "Skills available in this repo (1):\n  - demo-proj"

    def state_at(compact_version: int, seen: int) -> BaseAgentState:
        fields: dict[str, Any] = {
            "ava_code__cwd": str(tmp_path),
            "ava_code__project_skills_note": note,
            "ava_code__project_skills_seen_compact": seen,
        }
        return state_cls(
            messages=[], halted=False, compact=CompactState(version=compact_version), **fields
        )

    assert await inject_cwd_notes_after_exec(state_at(0, 0), None, None) is None
    result = await inject_cwd_notes_after_exec(state_at(1, 0), None, None)
    assert [tag for tag, _ in _hook_notes(result)] == ["project_skills"]
    assert result is not None
    assert result["ava_code__project_skills_seen_compact"] == 1
    assert "ava_code__cwd_note" not in result


async def test_after_exec_hook_is_noop_without_pending_notes(tmp_path: Path):
    from ava_builtins.plugins.ava_code.agent_runtime import inject_cwd_notes_after_exec

    assert (
        await inject_cwd_notes_after_exec(_make_state_with_cwd(str(tmp_path)), None, None) is None
    )


async def test_after_exec_hook_on_a_state_without_the_plugin_fields_fails_loudly():
    """No swallowed errors: a graph state that lacks the plugin's channels is a wiring bug and
    surfaces as one."""
    from ava_builtins.plugins.ava_code.agent_runtime import inject_cwd_notes_after_exec

    with pytest.raises(AttributeError):
        await inject_cwd_notes_after_exec(BaseAgentState(), None, None)
