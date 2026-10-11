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
) -> tuple[int, float] | None:
    """Log LangChain-standardized usage for one completed agent LLM call.

    `agent_id` is required accounting identity supplied by the completed turn.
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
        stamp_message=True,
    )
