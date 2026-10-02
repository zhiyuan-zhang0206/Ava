"""A hosted restart marker does not claim completion."""

from langchain_core.messages import HumanMessage

from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import _BatchState, _handle_restart
from base.agents.context import AvaContext
from base.agents.context.slices import AgentSlices


async def test_hosted_restart_marker_does_not_claim_completion() -> None:
    state = _BatchState()
    await _handle_restart(
        AvaContext(agent=AgentSlices.resolve()),
        1,
        ClaimedInbound(id=1, agent_id=1, content="", kind="restart", source="self", payload={}),
        state,
    )
    assert state.restart_requested
    assert isinstance(state.new_msgs[0], HumanMessage)
    assert (
        state.new_msgs[0].model_dump()["additional_kwargs"]["ava_note_tag"] == "lifecycle_restart"
    )
    content = state.new_msgs[0].model_dump()["content"]
    assert isinstance(content, str)
    assert "Restart was accepted" in content
    assert "have been restarted" not in content
