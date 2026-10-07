"""State slot cases: exec node preserves state update on lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage
from langgraph.types import Overwrite

import ava
from agent.graph.exec.node import _exec_node_impl
from agent.state import AttachEntry, AttachState, BaseAgentState
from agent.tests.test_state_slot import (
    _ai_message_with_code,
    _make_runtime_and_config,
    _messages_plugin_state_cls,
    _scan_flagged_code,
)
from agent.tests.test_state_slot import (
    _reset_state_slot as _reset_state_slot,
)
from base.agents.messages.security_finding import SecurityFindingEntry


async def test_exec_node_preserves_state_update_on_lifecycle(fake_cancel_event):
    """Lifecycle (terminate) path: plugin's state_update written before raising still merges.

    Directly raise AgentTermination to avoid `ava.self.terminate()`'s internal db INSERT
    (test doesn't mock ava.DB)——the invariant for this test is "plugin state_update in lifecycle
    path still merges into Command", not the full terminate flow."""
    from ava.self import AgentTermination

    assert AgentTermination is not None  # let import not be removed by ruff

    class _State(BaseAgentState):
        plugin__last_action: str = ""

    code = (
        "import ava\n"
        "ava.state_update['plugin__last_action'] = 'about-to-terminate'\n"
        "from ava.self import AgentTermination\n"
        "raise AgentTermination\n"
    )
    state = _State(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("plugin__last_action") == "about-to-terminate"  # pyright: ignore[reportUnknownMemberType]
    assert update["halted"] is True
    assert not ava.in_exec_turn()


async def test_exec_node_commits_security_finding_to_graph_state(fake_cancel_event):
    """A finding raised inside the exec child rides the state update into
    `state.security_findings`; the exec node itself adds no note to its messages
    delta (the after_exec hook delivers it) and the host keeps no findings of its own."""
    state = BaseAgentState(
        messages=[_ai_message_with_code(_scan_flagged_code("shell.run"))],
        halted=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    assert [m.type for m in update["messages"]] == ["tool"]
    assert update["messages"][0].tool_call_id == "call_1"
    assert update["security_findings"] == [
        SecurityFindingEntry(source="shell.run", triggers=["ignore previous instructions"])
    ]


async def test_exec_node_keeps_plugin_notes_after_toolmessage_beside_findings(fake_cancel_event):
    """Order in the exec's messages delta: ToolMessage, then the plugin's context
    notes — the tool_use adjacency is preserved — while the finding for the same
    context file goes to the graph-state channel."""
    state_cls = _messages_plugin_state_cls()

    code = (
        _scan_flagged_code("context-file:/repo/AGENTS.md")
        + "import ava\n"
        + "from langchain_core.messages import HumanMessage\n"
        + "ava.state_update['messages'] = [HumanMessage(content='project note', id='p1')]\n"
    )
    state = state_cls(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    msgs = update["messages"]
    assert [m.type for m in msgs] == ["tool", "human"], (
        f"expected [tool, plugin], got {[m.type for m in msgs]}"
    )
    assert msgs[1].id == "p1"
    assert msgs[1].content == "project note"
    assert [f.source for f in update["security_findings"]] == ["context-file:/repo/AGENTS.md"]


async def test_exec_node_checkpoints_child_attachment(fake_cancel_event, tmp_path: Path):
    """A normal child registration drains into a media message in the exec update.

    User ruling 2026-08-26: the attach message lands right after the exec
    output in the SAME turn, so the update must contain the packed media
    HumanMessage and a cleared attach channel — not parked pending entries
    for the claim boundary.
    """
    from base.agents.messages.kwargs import AvaMsgType

    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited AVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = f"import ava\nava.self.attach({str(image)!r}, label='render result')"
    state = BaseAgentState(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock(), {"llm_model": "claude-sonnet-5"})

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    # Pending is drained (cleared) in the same update — nothing parked.
    assert update["attach"] == AttachState()
    messages = update["messages"]
    assert len(messages) == 2
    attach_msg = messages[-1]
    assert isinstance(attach_msg, HumanMessage)
    assert attach_msg.additional_kwargs["ava_msg_type"] == AvaMsgType.ATTACH.value  # pyright: ignore[reportUnknownMemberType]
    # Interleaved pack: the notice leads, then the file's own caption line.
    # The exec-node context model here is a bare MagicMock (no media
    # capability), so the pack is caption-only — no media block.
    content = attach_msg.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, list)
    assert [cast("dict[str, Any]", b)["type"] for b in content] == ["text", "text"]
    assert "Files attached during this turn" in cast("dict[str, Any]", content[0])["text"]
    caption_block = cast("dict[str, Any]", content[1])
    assert "render.png" in caption_block["text"]


async def test_exec_node_compact_path_drops_notes_and_findings(fake_cancel_event, tmp_path: Path):
    """The compact path (SystemHalt) writes nothing back — claim REMOVE_ALLs
    the whole history — so neither plugin notes nor the child's findings may
    leak into the update (the findings channel is reset instead)."""

    state_cls = _messages_plugin_state_cls()
    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited AVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = (
        _scan_flagged_code("shell.run")
        + "import ava\n"
        + "from langchain_core.messages import HumanMessage\n"
        + "ava.state_update['messages'] = [HumanMessage(content='x')]\n"
        + f"ava.self.attach({str(image)!r})\n"
        + "from base.agents.lifecycle import SystemHalt\n"
        + "raise SystemHalt()\n"
    )
    state = state_cls(
        messages=[_ai_message_with_code(code)],
        halted=False,
        attach=AttachState(pending=[AttachEntry(path="/previous.png", label=None)]),
    )
    runtime, config = _make_runtime_and_config(AsyncMock(), {"llm_model": "claude-sonnet-5"})

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("messages") == [], (  # pyright: ignore[reportUnknownMemberType]
        f"compact path must write no messages back, got {update.get('messages')!r}"  # pyright: ignore[reportUnknownMemberType]
    )
    assert update.get("halted") is True  # pyright: ignore[reportUnknownMemberType]
    assert update["security_findings"] == Overwrite([])
    assert "attach" not in update
