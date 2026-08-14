"""Error-recovery scenario — LLM fails permanently, then recovers on the next turn.

turn 1: the fake raises FatalProviderError (a provider rejection class that is
        EXCLUDED from the llm node retry policy — agent/graph/_build.py
        `_should_retry` returns False for it). The agent loop aborts the turn,
        emits one SSE `error` event ("The turn was aborted; the agent is still
        alive and idling..."), and halts idling for the next inbound.
turn 2: normal reply — proves the agent recovered without a restart.

The frontend renders the SSE error event as an ephemeral `[error] ...` marker
(payload prefix "error:"), NOT as the unrecognized-marker red alarm — the
distinction Case 3 exists to lock in.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from agent.graph._llm_errors import FatalProviderError
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

RECOVERY_REPLY = "恢复正常了，刚才提供商拒绝了请求。"

ERROR_MSG = "scripted e2e provider rejection"


class ErrorThenRecoverFake(ScriptedFakeChatModel):
    """Raises FatalProviderError on the first LLM call, then plays the script.

    One-shot flag (not cursor-based): the fatal error aborts the turn before
    any message is consumed, so the cursor stays at 0 — a cursor check would
    re-raise on the recovery turn.
    """

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


def build(model: str) -> ScriptedFakeChatModel:
    return ErrorThenRecoverFake(script=RECOVERY_SCRIPT)
