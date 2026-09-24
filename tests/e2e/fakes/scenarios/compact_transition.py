"""Two compact boundaries with distinct replies for the display transition test."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

FIRST_REPLY = "TRANSITION_FIRST_REPLY"
FIRST_TAIL = "TRANSITION_FIRST_TAIL"
FIRST_NARRATION = "TRANSITION_FIRST_NARRATION"
BETWEEN_REPLY = "TRANSITION_BETWEEN_REPLY"
BETWEEN_TAIL = "TRANSITION_BETWEEN_TAIL"
SECOND_NARRATION = "TRANSITION_SECOND_NARRATION"

_SCRIPT = tuple(
    AIMessage(content=text, usage_metadata=_USAGE)
    for text in (
        FIRST_REPLY,
        FIRST_TAIL + " filler" * 400,
        "TRANSITION_FIRST_SUMMARY",
        FIRST_NARRATION,
        BETWEEN_REPLY,
        BETWEEN_TAIL + " filler" * 400,
        "TRANSITION_SECOND_SUMMARY",
        SECOND_NARRATION,
    )
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=_SCRIPT)
