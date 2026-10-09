"""ava_code plugin: the effects reach the model, not just "no error".

Every test runs a real agent turn (real gateway, agent host, exec child, hooks) with a
scripted model that records what it is handed (`fakes/scenarios/ava_code.py`). The
assertions are on three kinds of witness, never on a function's return value:

  - the messages the model actually received at each call (`model_inputs()`),
  - the files the exec really wrote (or did not write) on disk,
  - the agent's persisted checkpoint (plugin state).

Why this exists: the after_exec hook once returned early on an error the host
swallowed, so the cwd-change note and the project-skills listing were never injected,
and every test that only checked "no exception" stayed green. A test here fails if the
note is not in the model's input, whatever the code path says.

Not covered here: the ava.cwd / ava.files wraps' behavior outside a turn (unit tests in
`ava_builtins/plugins/ava_code/tests`), and the engineering-workflow prompt section
(config-gated off by default).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from base.config import settings
from base.paths import workspace_dir
from tests.components.base.poll_until import poll_until
from tests.e2e._db import (
    chat_and_wait,
    checkpoint_values,
    enqueue_compact_history_fixture,
    wait_for_status,
)
from tests.e2e._ports import GATEWAY_URL
from tests.e2e.fakes.scenarios import ava_code as world

Call = list[dict[str, Any]]
Values = dict[str, Any]


@pytest.fixture
def seeded_world() -> Iterator[None]:
    world.seed_world()
    try:
        yield
    finally:
        shutil.rmtree(world.root(), ignore_errors=True)


def _calls(expected: int) -> list[Call]:
    inputs = world.model_inputs()
    assert len(inputs) == expected, (
        f"expected {expected} model calls, saw {len(inputs)}; last call's messages: "
        f"{[(m['type'], m['text'][:120]) for m in inputs[-1]] if inputs else None}"
    )
    return inputs


def _notes(call: Call, tag: str) -> list[str]:
    """Texts of the framework system notes with this tag in one model call's input."""
    return [m["text"] for m in call if m["tag"] == tag]


def _context_headers(call: Call) -> list[str]:
    """`Project <file> from <path>` first lines of the injected context-file notes."""
    return [
        text.split("\n", 1)[0].removeprefix("[system] ").removesuffix(":")
        for text in _notes(call, "context")
        if text.startswith("[system] Project ")
    ]


def _tool_outputs(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "tool")


def _checkpoint(agent_id: int, ready: Callable[[Values], bool]) -> Values:
    """Poll for a checkpoint satisfying `ready` -- the turn's commit can trail its reply."""
    values: Values = {}

    def committed() -> tuple[bool, object]:
        nonlocal values
        values = checkpoint_values(agent_id)
        return bool(values) and ready(values), {k: v for k, v in values.items() if k != "messages"}

    poll_until(committed, timeout=30.0, interval=0.5, what=f"agent {agent_id} checkpoint")
    return values


def _assert_notes_follow_the_tool_result(after: Call, proj: str) -> None:
    """The call right after `ava.cwd.set`: the cwd note and the skills note are in the input,
    AFTER the tool result (the Anthropic wire contract forbids anything between
    tool_use and tool_result)."""
    cwd_notes = [t for t in _notes(after, "context") if "Working directory set to" in t]
    assert cwd_notes == [f"[system] Working directory set to {proj}"], (
        f"cwd-change note missing from the model's input after ava.cwd.set; notes seen: "
        f"{[(m['tag'], m['text'][:100]) for m in after if m['tag']]}"
    )
    skills_notes = _notes(after, "project_skills")
    assert len(skills_notes) == 1, f"project skills note not delivered: {skills_notes}"
    assert world.SKILL_NAME in skills_notes[0]
    assert world.SKILL_DESCRIPTION in skills_notes[0]
    assert f"{proj}/.claude/skills/{world.SKILL_NAME}" in skills_notes[0]
    last_tool = max(i for i, m in enumerate(after) if m["type"] == "tool")
    assert [m["tag"] for m in after[last_tool + 1 :]] == ["context", "project_skills"], (
        f"notes must follow the tool result in order; tail was "
        f"{[(m['type'], m['tag']) for m in after[last_tool:]]}"
    )


# -- cwd-change note + project skills note (the after_exec hook) -----------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_cwd_notes")
def test_cwd_switch_puts_cwd_note_and_project_skills_in_the_next_model_input(
    spawned_agent: int, seeded_world: None
) -> None:
    chat_and_wait(spawned_agent, "switch to the project")
    proj = str(world.project())
    calls = _calls(3)

    # Before the exec nothing about the project is in the model's input.
    assert not _notes(calls[0], "context") and not _notes(calls[0], "project_skills")

    _assert_notes_follow_the_tool_result(calls[1], proj)

    # The next call still has exactly one of each: consumed, not re-injected per exec.
    final = calls[2]
    assert sum("Working directory set to" in t for t in _notes(final, "context")) == 1
    assert len(_notes(final, "project_skills")) == 1

    # Persisted state agrees: cwd moved, the pending note was consumed and the
    # skills bookmark advanced.
    values = _checkpoint(spawned_agent, lambda v: v.get("ava_code__cwd_note") is None)
    assert values["ava_code__cwd"] == proj
    assert values["ava_code__cwd_note"] is None, "cwd_note was never consumed"
    assert values["ava_code__project_skills_seen_compact"] == 0


def _assert_oversized_note_is_head_and_tail(call: Call, already: list[str]) -> None:
    """An oversized AGENTS.md is injected head + tail only, with the full text archived."""
    headers = _context_headers(call)
    assert headers[:2] == already and len(headers) == 3, headers
    note = next(t for t in _notes(call, "context") if world.BIG_HEAD in t)
    assert world.BIG_TAIL in note
    assert world.BIG_MID not in note
    assert "output truncated" in note
    archive = re.search(r"full output at (\S+?)[ ;\]]", note)
    assert archive, f"truncated note names no archive path: {note[:400]!r}"
    archived = Path(archive.group(1)).read_text()
    assert world.BIG_MID in archived
    assert world.BIG_HEAD in archived


# -- context-file (AGENTS.md / CLAUDE.md) injection on read ----------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_context_files")
def test_reading_a_file_injects_agents_and_claude_md_once(
    spawned_agent: int, seeded_world: None
) -> None:
    chat_and_wait(spawned_agent, "read around")
    calls = _calls(7)
    proj = world.project()
    root_agents = f"Project AGENTS.md from {proj / 'AGENTS.md'}"
    sub_claude = f"Project CLAUDE.md from {proj / 'sub' / 'CLAUDE.md'}"

    assert _context_headers(calls[0]) == []

    # Read proj/sub/foo.py: the AGENTS.md at the repo root and the CLAUDE.md next to
    # it are both delivered, farthest first, with their real bodies.
    first = calls[1]
    assert _context_headers(first) == [root_agents, sub_claude], (
        f"context files not injected on read; saw {_context_headers(first)}; "
        f"tool output: {_tool_outputs(first)!r}"
    )
    bodies = "\n".join(_notes(first, "context"))
    assert world.ROOT_RULES in bodies and world.SUB_CLAUDE in bodies

    # A second read in the same tree adds nothing (path dedup).
    assert _context_headers(calls[2]) == [root_agents, sub_claude]

    # Reading an AGENTS.md directly: the content comes back as the return value and is
    # NOT also injected as a note; later reads in that repo do not inject it either.
    assert world.PRIMARY_PATH_RULES in _tool_outputs(calls[3])
    assert _context_headers(calls[3]) == [root_agents, sub_claude]
    assert _context_headers(calls[4]) == [root_agents, sub_claude]
    assert not any(world.PRIMARY_PATH_RULES in m["text"] for m in calls[4] if m["tag"])

    # Same AGENTS.md content from another path: content-hash dedup, no second copy.
    assert _context_headers(calls[5]) == [root_agents, sub_claude]

    _assert_oversized_note_is_head_and_tail(calls[6], [root_agents, sub_claude])

    # Persisted dedup state names what was surfaced.
    values = _checkpoint(spawned_agent, lambda v: bool(v.get("ava_code__injected_paths")))
    assert str(proj / "AGENTS.md") in values["ava_code__injected_paths"]


# -- relative paths follow the logical cwd ---------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_cwd_tools")
def test_sdk_calls_resolve_relative_paths_against_the_logical_cwd(
    spawned_agent: int, seeded_world: None
) -> None:
    chat_and_wait(spawned_agent, "work in the project")
    out = _tool_outputs(_calls(2)[1])
    sub = world.project() / "sub"

    # Observable world: the file landed in the project subdir, nothing in the workspace.
    assert (sub / "out.txt").read_text() == "ALPHA\nbeta\n", f"exec output: {out!r}"
    assert not (sub / "gone.txt").exists()
    workspace = workspace_dir(spawned_agent)
    assert not (workspace / "out.txt").exists() and not (workspace / "gone.txt").exists()

    # What the exec reported about itself agrees with the disk.
    assert "set-error FileNotFoundError" in out
    assert "set-error NotADirectoryError" in out
    assert "set-unexpected-ok" not in out
    assert f"cwd {sub}" in out
    assert "process-cwd-follows False" in out
    assert "glob ['out.txt']" in out
    assert f"shell-pwd {sub}" in out

    values = _checkpoint(spawned_agent, lambda v: v.get("ava_code__cwd") == str(sub))
    assert values["ava_code__cwd"] == str(sub)


# -- cwd survives a restart; a vanished cwd falls back ---------------------------


def _restart_and_wait(agent_id: int) -> None:
    httpx.post(f"{GATEWAY_URL}/api/agents/{agent_id}/restart", timeout=10.0).raise_for_status()

    def applied() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT applied_at IS NOT NULL FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            rows = cur.fetchall()
        return rows == [(True,)], rows

    poll_until(applied, timeout=90.0, interval=0.5, what=f"agent {agent_id} restart applied")
    wait_for_status(agent_id, "idling")


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_cwd_restart")
def test_cwd_survives_a_restart(spawned_agent: int, seeded_world: None) -> None:
    chat_and_wait(spawned_agent, "move to sub")
    sub = str(world.project() / "sub")
    _checkpoint(spawned_agent, lambda v: v.get("ava_code__cwd") == sub)
    _restart_and_wait(spawned_agent)

    chat_and_wait(spawned_agent, "where are you now")
    calls = world.model_inputs()
    # The new process's own exec reports the persisted cwd.
    assert f"cwd-after-restart {sub}" in _tool_outputs(calls[-1]), (
        f"cwd lost across restart; model saw: {_tool_outputs(calls[-1])!r}"
    )
    assert (
        _checkpoint(spawned_agent, lambda v: v.get("ava_code__cwd") == sub)["ava_code__cwd"] == sub
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_cwd_restart")
def test_a_vanished_cwd_falls_back_to_the_workspace_after_restart(
    spawned_agent: int, seeded_world: None
) -> None:
    chat_and_wait(spawned_agent, "move to sub")
    sub = world.project() / "sub"
    _checkpoint(spawned_agent, lambda v: v.get("ava_code__cwd") == str(sub))
    shutil.rmtree(sub)  # e.g. the worktree was removed after its PR merged
    _restart_and_wait(spawned_agent)

    chat_and_wait(spawned_agent, "where are you now")
    fallback = str(workspace_dir(spawned_agent).resolve())
    out = _tool_outputs(world.model_inputs()[-1])
    assert f"cwd-after-restart {fallback}" in out, (
        f"stale cwd not repaired: expected the workspace {fallback}; model saw: {out!r}"
    )
    values = _checkpoint(
        spawned_agent, lambda v: Path(v.get("ava_code__cwd", "")).resolve() == Path(fallback)
    )
    assert Path(values["ava_code__cwd"]).resolve() == Path(fallback)


# -- the coding conventions reach the system prompt ------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_system_prompt")
def test_system_prompt_carries_the_coding_conventions(
    spawned_agent: int, seeded_world: None
) -> None:
    chat_and_wait(spawned_agent, "hello")
    first = _calls(1)[0]
    system = first[0]
    assert system["type"] == "system", [m["type"] for m in first]
    prompt = system["text"]
    assert "# Coding tools" in prompt
    # The worktree + PR workflow is a prompt promise of this plugin.
    assert "git worktree" in prompt
    assert "Ava-<your agent id>" in prompt
    assert "Read `AGENTS.md` first" in prompt
    assert "ava.cwd" in prompt
    # Off by default: the opt-in debugging advice is not injected.
    assert "Resolving issues and debugging" not in prompt


# -- compaction makes the notes resurface ----------------------------------------


def _compact_and_read_again(agent_id: int) -> tuple[Call, Call]:
    """Run the compact scenario; return the model input before the compact and after it."""
    chat_and_wait(agent_id, "set up the project")
    before = _calls(2)[1]
    enqueue_compact_history_fixture(agent_id)
    # The compaction costs two more model calls: the summary, then the post-compact narration.
    poll_until(
        lambda: (len(world.model_inputs()) == 4, len(world.model_inputs())),
        timeout=60.0,
        interval=0.5,
        what="compaction summary + narration model calls",
    )
    wait_for_status(agent_id, "idling")
    chat_and_wait(agent_id, "read bar")
    return before, _calls(6)[5]


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_after_compact")
def test_compaction_resurfaces_context_files(spawned_agent: int, seeded_world: None) -> None:
    before, after = _compact_and_read_again(spawned_agent)
    proj = world.project()
    expected = [
        f"Project AGENTS.md from {proj / 'AGENTS.md'}",
        f"Project CLAUDE.md from {proj / 'sub' / 'CLAUDE.md'}",
    ]
    assert _context_headers(before) == expected
    assert _context_headers(after) == expected, (
        f"context files not re-surfaced after compact; saw {_context_headers(after)}"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.ava_code:build_after_compact")
def test_compaction_resurfaces_project_skills(spawned_agent: int, seeded_world: None) -> None:
    before, after = _compact_and_read_again(spawned_agent)
    assert len(_notes(before, "project_skills")) == 1
    assert len(_notes(after, "project_skills")) == 1, (
        f"project skills not re-injected after compact; notes: "
        f"{[(m['tag'], m['text'][:80]) for m in after if m['tag']]}"
    )
