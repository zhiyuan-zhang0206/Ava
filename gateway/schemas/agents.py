"""Selected-agent lifecycle detail returned by the gateway."""

from shared.agents.observation.snapshot import AgentSnapshot


class AgentRow(AgentSnapshot):
    """GET /api/agents/{id} detail, including response-required notice bodies.

    The directory and live roster use bounded cards from shared.agents.observation.roster.
    last_active_at is the real-activity clock; last_inbound_at is the latest
    inbound message clock.
    """
