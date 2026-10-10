"""Main-conversation compatibility facade for canonical LLM usage accounting."""

from datetime import datetime

from langchain_core.messages import AIMessage

from base.lm.catalog import ModelCatalog
from base.lm.usage import log_usage_from_message


def log_llm_usage(
    msg: AIMessage,
    model: str,
    *,
    agent_id: int | None,
    catalog: ModelCatalog,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    usage_kind: str = "agent",
    cache_mechanism: str | None = None,
    cache_scope: str | None = None,
) -> tuple[int, float] | None:
    """Log LangChain-standardized usage for one completed agent LLM call.

    `agent_id` is required accounting identity supplied by the completed turn.
    `cache_mechanism` / `cache_scope` label how much of the provider's
    cache_read field covers (base/lm/usage.py constants): the llm node
    passes mixed/explicit_block when the Gemini explicit cache carried the
    request, because the API then reports only the explicit block.
    """
    if agent_id is None:
        raise ValueError("agent LLM usage requires an explicit agent id")
    return log_usage_from_message(
        msg,
        model,
        catalog=catalog,
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        priced_at=priced_at,
        usage_kind=usage_kind,
        for_agent_id=agent_id,
        cache_mechanism=cache_mechanism,
        cache_scope=cache_scope,
        stamp_message=True,
    )
