from __future__ import annotations

from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from agent.graph.llm_errors import FatalProviderError
from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

RECOVERY_REPLY = "\u6062\u590d\u6b63\u5e38\u4e86\uff0c\u521a\u624d\u63d0\u4f9b\u5546\u62d2\u7edd\u4e86\u8bf7\u6c42\u3002"

ERROR_MSG = "scripted e2e provider rejection"


class ErrorThenRecoverFake(ScriptedFakeChatModel):
    _errored: bool = PrivateAttr(default=False)

    def _next_message(self) -> AIMessage:
        if not self._errored:
            self._errored = True
            raise FatalProviderError(
                ERROR_MSG,
                error_class="permanent",
                provider="e2e-fake",
                status=402,
            )
        return super()._next_message()


RECOVERY_SCRIPT: tuple[AIMessage, ...] = (AIMessage(content=RECOVERY_REPLY, usage_metadata=_USAGE),)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ErrorThenRecoverFake(script=RECOVERY_SCRIPT)
