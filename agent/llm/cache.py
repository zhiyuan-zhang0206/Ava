"""Provider-neutral call preparation and one plain recovery within the Core timeout."""

from __future__ import annotations

import asyncio
from typing import cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import Runnable

from agent.llm import execute_code
from base.config import settings
from base.host.env.agent_slices import LlmCallPolicy
from base.lm.call import LlmInvocation, ProviderCallContext, recover_invocation
from base.lm.provider_api import ProviderBinding


async def prepare_invocation(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    policy: LlmCallPolicy,
    binding: ProviderBinding | None = None,
) -> LlmInvocation:
    """Ask the actual build's adapter, or use ordinary tool binding."""
    if binding is not None and binding.prepare_call is not None:
        prepared = await binding.prepare_call(
            ProviderCallContext(llm, list(messages), [execute_code], policy)
        )
        if prepared is not None:
            if not isinstance(prepared, LlmInvocation):
                raise TypeError("provider preparation must return an invocation or None")
            if not callable(getattr(prepared.runnable, "ainvoke", None)):
                raise TypeError("prepared invocation runnable must support ainvoke")
            return prepared
    return LlmInvocation(runnable=llm.bind_tools([execute_code]), messages=list(messages))


async def ainvoke_with_cache_retry(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    policy: LlmCallPolicy,
    *,
    binding: ProviderBinding | None = None,
    retry_stale_cache: bool = True,
) -> tuple[AIMessage, bool]:
    """Invoke with at most one adapter recovery and successful-attempt provenance.

    The total Core deadline covers preparation, the first wire attempt and
    recovery. Single-attempt callers disable recovery; cancellation propagates.
    """
    if type(retry_stale_cache) is not bool:
        raise ValueError("retry_stale_cache must be a boolean")

    async def _invoke() -> tuple[AIMessage, bool]:
        invocation = await prepare_invocation(llm, messages, policy, binding)
        try:
            response = await cast(
                Runnable[list[AnyMessage], AIMessage], invocation.runnable
            ).ainvoke(invocation.messages)
        except Exception as exc:
            plain = recover_invocation(invocation, exc) if retry_stale_cache else None
            if plain is None:
                raise
            invocation = plain
            response = await cast(Runnable[list[AnyMessage], AIMessage], plain.runnable).ainvoke(
                plain.messages
            )
        assert isinstance(response, AIMessage)  # noqa: S101 — chat models return AIMessage
        return response, invocation.used_explicit_cache

    return await asyncio.wait_for(_invoke(), timeout=settings.lm.llm_compact_timeout_seconds)
