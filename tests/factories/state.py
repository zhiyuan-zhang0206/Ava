"""AgentState factory."""

from langchain_core.messages import AnyMessage

from agent.state import AgentState


def state_with_messages(*msgs: AnyMessage) -> AgentState:
    """Construct AgentState with specified messages."""
    return AgentState(messages=list(msgs))


def empty_state() -> AgentState:
    """Construct empty AgentState."""
    return AgentState(messages=[])


def halted_state(*msgs: AnyMessage) -> AgentState:
    """Construct AgentState with halted=True."""
    return AgentState(messages=list(msgs), halted=True)
