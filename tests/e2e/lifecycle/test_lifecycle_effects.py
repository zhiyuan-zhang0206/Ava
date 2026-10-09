"""Agent lifecycle: each action's effect is witnessed, not just that the call returned.

Real agents (gateway, agent host, exec child) with a recording scripted model
(`fakes/scenarios/lifecycle_effects.py`). Witnesses: what each agent's model was handed,
inbound rows and agent status in the database, the process table, the checkpoint.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

from base.config import settings
from tests.components.base.poll_until import poll_until
from tests.e2e._db import chat_and_wait, checkpoint_values, wait_for_status
from tests.e2e._ports import GATEWAY_URL
from tests.e2e.fakes._recording import model_inputs, reset_record, scratch_root
from tests.e2e.fakes.scenarios import lifecycle_effects as world

Call = list[dict[str, Any]]


@pytest.fixture
def clean_record() -> Iterator[None]:
    reset_record()
    shutil.rmtree(scratch_root("lifecycle"), ignore_errors=True)
    scratch_root("lifecycle").mkdir(parents=True)
    yield
    shutil.rmtree(scratch_root("lifecycle"), ignore_errors=True)
    reset_record()


def _human_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "human")


def _tool_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "tool")


def _inbound(agent_id: int) -> list[tuple[str, str, str]]:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _terminate(agent_id: int) -> None:
    with contextlib.suppress(Exception):
        httpx.post(f"{GATEWAY_URL}/api/agents/{agent_id}/terminate", timeout=5.0)
        wait_for_status(agent_id, "terminated", timeout=15.0)


def _spawn_idle_agent() -> int:
    resp = httpx.post(f"{GATEWAY_URL}/api/agents", json={"spawner": "user"}, timeout=60.0)
    resp.raise_for_status()
    return int(resp.json()["id"])


def _wait_for_model_calls(agent_id: int, n: int, what: str) -> None:
    poll_until(
        lambda: (len(model_inputs(agent_id)) >= n, len(model_inputs(agent_id))),
        timeout=60.0,
        interval=0.3,
        what=what,
    )


# -- agent-to-agent message ----------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_relay")
def test_sdk_message_reaches_the_other_agents_model(spawned_agent: int, clean_record: None) -> None:
    sender = spawned_agent
    peer = _spawn_idle_agent()
    try:
        scratch_root("lifecycle").joinpath("peer_id").write_text(str(peer))
        chat_and_wait(sender, "relay a ping")
        _wait_for_model_calls(peer, 1, f"peer {peer} woken by the message")

        # The peer's model was handed the text, and the inbound names the sender.
        assert world.PING in _human_text(model_inputs(peer)[0]), (
            f"peer's first model input lacks the message: {model_inputs(peer)[0][-3:]}"
        )
        assert ("chat", f"agent:{sender}", "done") in _inbound(peer) or (
            "chat",
            f"agent:{sender}",
            "claimed",
        ) in _inbound(peer), _inbound(peer)
        # The sender saw its own exec confirm the send; it did not get the peer's reply
        # as a side channel (messages are not return values).
        assert f"sent-to {peer}" in _tool_text(model_inputs(sender)[1])
    finally:
        _terminate(peer)


# -- SDK spawn -----------------------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_spawn")
def test_sdk_spawn_starts_a_child_that_runs_the_prompt(
    spawned_agent: int, clean_record: None
) -> None:
    parent = spawned_agent
    chat_and_wait(parent, "spawn a child")
    match = re.search(r"child-id (\d+)", _tool_text(model_inputs(parent)[1]))
    assert match, f"spawn printed no child id: {_tool_text(model_inputs(parent)[1])!r}"
    child = int(match.group(1))
    try:
        assert child != parent
        _wait_for_model_calls(child, 1, f"child {child} runs its first turn")
        assert world.CHILD_PROMPT in _human_text(model_inputs(child)[0])
        assert ("chat", f"agent:{parent}") in [(k, s) for k, s, _ in _inbound(child)], _inbound(
            child
        )
        # The row in the agent directory is real and reachable through the REST API.
        got = httpx.get(f"{GATEWAY_URL}/api/agents/{child}", timeout=10.0)
        got.raise_for_status()
        assert got.json()["agent_id"] == child
    finally:
        _terminate(child)


# -- external terminate --------------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_terminate")
def test_external_terminate_stores_a_message_that_greets_the_revival(
    spawned_agent: int, clean_record: None
) -> None:
    agent = spawned_agent
    chat_and_wait(agent, "hello")
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent}/terminate",
        json={"message": world.TERMINATE_NOTE},
        timeout=10.0,
    ).raise_for_status()
    wait_for_status(agent, "terminated")
    assert any(k == "terminate" and s == "user" for k, s, _ in _inbound(agent)), _inbound(agent)
    # Terminating alone costs no model call: the note is stored, not answered.
    assert len(model_inputs(agent)) == 1

    # A new message revives the same agent; its model is handed the stored note.
    chat_and_wait(agent, "are you back")
    revived = model_inputs(agent)[1]
    assert world.TERMINATE_NOTE in _human_text(revived), (
        f"terminate message not delivered to the revived agent: {_human_text(revived)[-600:]!r}"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_cancel")
def test_forced_terminate_kills_a_running_exec_and_its_process(
    spawned_agent: int, clean_record: None
) -> None:
    agent = spawned_agent
    _start_long_exec(agent)
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent}/terminate", json={"force": True}, timeout=10.0
    ).raise_for_status()
    wait_for_status(agent, "terminated", timeout=30.0)
    _assert_exec_never_finishes()


# -- cancel --------------------------------------------------------------------


def _assert_exec_never_finishes() -> None:
    """Wait past the exec's sleep: an exec that survived would write its marker by now."""
    time.sleep(world.LONG_SLEEP_SECONDS)
    marker = world.exec_finished_marker()
    assert not Path(marker).exists(), "the cancelled exec kept running and finished"


def _start_long_exec(agent_id: int) -> None:
    """Send the message that makes the agent run its long exec; return once it is mid-turn."""
    httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/messages",
        json={"content": "run the long job", "source": "user"},
        timeout=10.0,
    ).raise_for_status()
    _wait_for_model_calls(agent_id, 1, "model asked for the long exec")
    time.sleep(3.0)  # let the exec child start sleeping


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_cancel")
def test_cancel_interrupts_a_running_exec_and_the_agent_stays_usable(
    spawned_agent: int, clean_record: None
) -> None:
    agent = spawned_agent
    _start_long_exec(agent)
    from base.agents.messages.native_cancel import accept_native_cancel, observe_native_work
    from base.db import Database, pool, publish_inbound_wake
    from base.events.live.bus import EventBus

    # This stack disables HTTP auth; drive the canonical native admission owner
    # as a trusted test producer. HTTP credential admission has gateway tests.
    with pool(max_size=2) as command_pool:
        target = observe_native_work(command_pool, agent)
        assert target is not None
        accepted = accept_native_cancel(command_pool, str(uuid4()), agent, target)
    publish_inbound_wake(
        Database.from_settings(), EventBus.from_settings(), agent, str(accepted.command_id)
    )

    def settled() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            row = conn.execute(
                "SELECT outcome FROM native_cancel_commands WHERE id=%s", (accepted.command_id,)
            ).fetchone()
        return row is not None and row[0] == "applied", row

    poll_until(settled, timeout=30.0, interval=0.5, what="cancel finalized")
    wait_for_status(agent, "idling", timeout=30.0)
    _assert_exec_never_finishes()

    chat_and_wait(agent, "are you still there")
    assert any("are you still there" in _human_text(c) for c in model_inputs(agent))


# -- agent-initiated compact ---------------------------------------------------


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_effects:build_self_compact")
def test_self_compact_replaces_history_with_the_summary(
    spawned_agent: int, clean_record: None
) -> None:
    agent = spawned_agent
    chat_and_wait(agent, "ORIGINAL-REQUEST-MARK do the compaction")
    calls = model_inputs(agent)
    assert len(calls) == 2, f"expected the pre-compact call and one after it, saw {len(calls)}"
    after = _human_text(calls[1])
    assert world.COMPACT_SUMMARY in after, f"summary absent from the next input: {after[-600:]!r}"
    assert "ORIGINAL-REQUEST-MARK" not in after, "raw history survived the compact"

    def committed() -> tuple[bool, object]:
        texts = [str(m.content) for m in checkpoint_values(agent).get("messages", [])]
        return any(world.COMPACT_SUMMARY in t for t in texts), len(texts)

    poll_until(committed, timeout=30.0, interval=0.5, what="compacted history checkpointed")
    texts = [str(m.content) for m in checkpoint_values(agent)["messages"]]
    assert not any("ORIGINAL-REQUEST-MARK" in t for t in texts)
