from langchain_core.messages import AnyMessage

from agent.state import AgentState


def state_with_messages(*msgs: AnyMessage) -> AgentState:
    return AgentState(messages=list(msgs))


def empty_state() -> AgentState:
    return AgentState(messages=[])


def halted_state(*msgs: AnyMessage) -> AgentState:
    return AgentState(messages=list(msgs), halted=True)
