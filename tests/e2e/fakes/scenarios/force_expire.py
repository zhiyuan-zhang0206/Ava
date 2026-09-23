"""One ordinary turn followed by the turn resumed after a takeover ends."""

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
FIRST_REPLY = "Ready for the external session."
RESUMED_REPLY = "I resumed my work after the external session ended."


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(
        script=(
            AIMessage(content=FIRST_REPLY, usage_metadata=_USAGE),
            AIMessage(content=RESUMED_REPLY, usage_metadata=_USAGE),
        )
    )
