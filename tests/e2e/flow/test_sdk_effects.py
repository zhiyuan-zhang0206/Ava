"""execute_code and SDK calls: the agent sees the effect.

Real agents with a recording scripted model (`fakes/scenarios/sdk_effects.py`). Witnesses:
the tool output and framework notes the model is handed next, files on disk, inbound rows.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from base.paths import ava_home
from tests.components.base.poll_until import poll_until
from tests.e2e._db import chat_and_wait
from tests.e2e._ports import GATEWAY_URL
from tests.e2e.fakes._recording import model_inputs, reset_record
from tests.e2e.fakes.scenarios import sdk_effects as world

Call = list[dict[str, Any]]


@pytest.fixture
def clean_world() -> Iterator[None]:
    reset_record()
    shutil.rmtree(world.sandbox(), ignore_errors=True)
    world.sandbox().mkdir(parents=True)
    yield
    shutil.rmtree(world.sandbox(), ignore_errors=True)
    reset_record()


def _tool_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "tool")


def _human_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "human")


def _last_tool(call: Call) -> str:
    tools = [m["text"] for m in call if m["type"] == "tool"]
    return tools[-1] if tools else ""


# -- exec failure containment --------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_exec_failures")
def test_a_raising_or_crashing_exec_is_reported_and_the_agent_carries_on(
    spawned_agent: int, clean_world: None
) -> None:
    chat_and_wait(spawned_agent, "break things")
    calls = model_inputs(spawned_agent)
    assert len(calls) == 4, f"expected 4 model calls, saw {len(calls)}"

    raised = _last_tool(calls[1])
    assert world.BOOM in raised and "RuntimeError" in raised, raised
    assert "Traceback" in raised, f"no traceback handed to the model: {raised!r}"

    # A child that dies without a Python exception still yields a tool result.
    crashed = _last_tool(calls[2])
    assert "[exec crashed:" in crashed, f"crash not reported to the model: {crashed!r}"

    # The next exec runs in a fresh child and works.
    assert world.ALIVE in _last_tool(calls[3])


# -- ava.files edit contract + injection scan ----------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_files_edges")
def test_files_edit_reports_missing_and_ambiguous_matches_and_read_flags_injection(
    spawned_agent: int, clean_world: None
) -> None:
    chat_and_wait(spawned_agent, "exercise files")
    calls = model_inputs(spawned_agent)
    assert len(calls) == 3
    out = _last_tool(calls[1])

    assert "edit-error zz | old not found" in out, out
    assert "edit-error aa | old appears 2 times" in out, out
    assert "edit-unexpected-ok" not in out
    assert "edited XX bb XX" in out, out
    assert (world.sandbox() / "edit.txt").read_text() == "XX bb XX"

    # Reading content that looks like an injection hands the model a SECURITY note.
    security = [m["text"] for m in calls[2] if m["tag"] == "security"]
    assert len(security) == 1, f"expected one security note, saw {security}"
    assert "inject.txt" in security[0], security[0]
    # The note names the source and triggers, never the file body.
    assert world.INJECTION not in security[0]


# -- composer commands ---------------------------------------------------------


@pytest.fixture
def probe_command() -> Iterator[None]:
    path = ava_home() / "commands" / f"{world.COMMAND_NAME}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ndescription: E2E probe command\ninstruction-hint: <question>\n---\n\n"
        f"{world.COMMAND_BODY}\n"
    )
    yield
    path.unlink(missing_ok=True)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_command")
def test_slash_command_expands_into_the_model_input(
    spawned_agent: int, clean_world: None, probe_command: None
) -> None:
    chat_and_wait(spawned_agent, f"/{world.COMMAND_NAME} what is the date")
    calls = model_inputs(spawned_agent)
    first = _human_text(calls[0])
    assert world.COMMAND_BODY in first, f"command not expanded for the model: {first[-500:]!r}"
    assert "what is the date" in first, "the free text after the command was dropped"
    # And the SDK lists it for peers.
    assert world.COMMAND_NAME in _last_tool(calls[1]), _last_tool(calls[1])


# -- user uploads --------------------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_upload")
def test_uploaded_file_is_announced_to_the_agent_and_readable(
    spawned_agent: int, clean_world: None
) -> None:
    agent = spawned_agent
    saved_dir = Path.home() / "Downloads" / f"AvaAgent-{agent}"
    try:
        resp = httpx.post(
            f"{GATEWAY_URL}/api/agents/{agent}/uploads",
            files=[("files", (world.UPLOAD_NAME, world.UPLOAD_TEXT.encode(), "text/plain"))],
            timeout=30.0,
        )
        resp.raise_for_status()
        saved = saved_dir / world.UPLOAD_NAME
        assert saved.read_text() == world.UPLOAD_TEXT

        poll_until(
            lambda: (len(model_inputs(agent)) >= 2, len(model_inputs(agent))),
            timeout=60.0,
            interval=0.5,
            what="agent turn triggered by the upload",
        )
        calls = model_inputs(agent)
        # The agent was told where the file is, and read it back from there.
        assert str(saved) in _human_text(calls[0]), _human_text(calls[0])[-500:]
        assert f"upload-content {world.UPLOAD_TEXT}" in _last_tool(calls[1])
    finally:
        shutil.rmtree(saved_dir, ignore_errors=True)


# -- exec timeout ---------------------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_timeout")
def test_exec_timeout_kills_the_whole_process_tree_and_tells_the_model(
    spawned_agent: int, clean_world: None
) -> None:
    chat_and_wait(spawned_agent, "run something too long", timeout=150.0)
    calls = model_inputs(spawned_agent)
    assert len(calls) == 2
    report = _last_tool(calls[1])
    assert "timed out" in report.lower() or "timeout" in report.lower(), report
    # Both the exec child and the process it spawned are gone.
    pids = [
        int(p.read_text())
        for p in (world.sandbox() / "child.pid", world.sandbox() / "grandchild.pid")
    ]
    alive = [
        pid
        for pid in pids
        if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    ]
    poll_until(
        lambda: (
            not [
                p
                for p in pids
                if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE
            ],
            alive,
        ),
        timeout=15.0,
        interval=0.5,
        what=f"timed-out exec processes {pids} die",
    )
