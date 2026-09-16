"""Preview data must satisfy the browser's current read contracts."""

from scripts.post_deploy_visual_fixtures import AGENT, AGENT_CARD
from scripts.post_deploy_visual_matrix import fixture_for
from shared.agent_roster import AgentCard, AgentDirectoryPage, AgentRoster
from shared.agent_snapshot import AgentSnapshot


def test_preview_agent_reads_resolve_roster_directory_and_selected_detail() -> None:
    """A missing roster fixture leaves selection empty and hides the inspector."""
    roster = AgentRoster.model_validate(fixture_for("/api/agents/roster"))
    directory = AgentDirectoryPage.model_validate(fixture_for("/api/agents"))
    detail = AgentSnapshot.model_validate(fixture_for("/api/agents/1"))

    assert len(roster.agents) == 1
    assert roster.agents[0].agent_id == detail.agent_id == 1
    assert roster.ancestors == []
    assert directory.agents == roster.agents
    assert directory.next_cursor is None
    assert set(AGENT) == set(AgentSnapshot.model_fields)
    assert set(AGENT_CARD) == set(AgentCard.model_fields)
    assert "notices_awaiting_response" not in AGENT_CARD
    assert "awaiting_response_count" in AGENT_CARD
