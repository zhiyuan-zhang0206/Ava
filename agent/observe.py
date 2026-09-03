"""Main-conversation compatibility facade for canonical LLM usage accounting."""

from datetime import datetime

from langchain_core.messages import AIMessage

from shared.lm.usage import log_usage_from_message


def log_llm_usage(
    msg: AIMessage,
    model: str,
    *,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    task_id: int | None = None,
    usage_kind: str = "agent",
) -> tuple[int, float] | None:
    """Log LangChain-standardized usage for one completed agent LLM call."""
    return log_usage_from_message(
        msg,
        model,
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        priced_at=priced_at,
        task_id=task_id,
        usage_kind=usage_kind,
    )
