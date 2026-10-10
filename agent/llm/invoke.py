"""One tool-bound model invocation within the compaction deadline."""

from __future__ import annotations

import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage

from agent.llm import execute_code
from base.config import settings
from base.lm.errors import normalize_provider_transport_error


async def ainvoke_tool_call(llm: BaseChatModel, messages: list[AnyMessage]) -> AIMessage:
    """Bind the agent tool and invoke once; transport failures are normalized.

    The complete message prefix stays in-band. The caller owns any retry policy;
    cancellation and unexpected failures propagate.
    """

    async def invoke() -> AIMessage:
        runnable = llm.bind_tools([execute_code])
        try:
            return await runnable.ainvoke(messages)
        except Exception as exc:
            normalized = normalize_provider_transport_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc

    return await asyncio.wait_for(invoke(), timeout=settings.lm.llm_compact_timeout_seconds)
