"""The provider-call layer of the understanding calls (chunk and upper-level grouping).

`build_generation_llm` builds the model the way the agent builds its own (so the provider
matches the request against the agent's prompt cache); `_invoke_agent_shaped` sends a finished
agent-shaped request through the tool-bound model: the request carries the agent's real tool
schema for cache parity, so a response that calls a tool is refused with an error result and
re-invoked a bounded number of times. `ModelCall` is the raw record of each provider call, handed to
the caller's `on_call`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from base.host.env.agent_slices import ModelOverrides
from base.lm.call import extract_text, invoke_response


class GenerateError(Exception):
    """A generation failure: the provider call failed after retries, or the reply was unusable."""


@dataclass(frozen=True)
class GenParams:
    """Generation calibration of one understanding request."""

    # Bounded tool-call refusal rounds for the agent-shaped request: the request carries the real
    # tool schema, so a response may call the tool instead of writing text; each such response is
    # refused with an error result and re-invoked, and exhausting the rounds fails the call —
    # never an unbounded loop.
    tool_rounds: int = 3
    # None = the model's own resolved effort, exactly what the agent's build
    # (`build_chat_model(model)`) uses — the reasoning parameters are part of the request the
    # provider matches its prompt cache against, so an override here would trade cache hits for a
    # cheaper summarizer.
    reasoning_effort: str | None = None


# The understanding calls retry a rate limit or a 5xx more than other generation paths: the
# consumer starts every due job at once and leaves rate limiting to the provider's 429 and this
# backoff (2 s doubling to 30 s, `Retry-After` honoured).
UNDERSTANDING_RETRY_ATTEMPTS = 5

# The refusal handed back when a response does carry tool calls: tools cannot run on this call,
# and the only correct continuation is the text answer.
_TOOL_REFUSAL = (
    "Error: tools are unavailable in this call. Retry by writing the requested "
    "summary as plain text — no tool calls."
)


def build_generation_llm(
    model: str,
    params: GenParams | None = None,
    overrides: ModelOverrides | None = None,
    *,
    thinking_off: bool = False,
) -> Any:
    """Build the generation chat model the way the agent builds its own.

    The single source of the understanding calls' model construction, so every path reaches the
    same provider shape and effort (the model's resolved effort, unless the caller passes
    `params.reasoning_effort`); `overrides` is the target agent's tuning pins
    (`agent_model_target`), so a pinned reasoning effort or thinking budget is the agent's
    own; `thinking_off` builds it with thinking disabled where the provider allows it.
    """
    from base.lm.factory import build_chat_model

    p = params or GenParams()
    return build_chat_model(
        model,
        reasoning_effort=p.reasoning_effort,
        overrides=overrides,
        thinking={"type": "disabled"} if thinking_off else None,
    )


@dataclass(frozen=True)
class ModelCall:
    """One provider call made by `_invoke_agent_shaped`, as handed to `on_call`.

    `round` counts the calls of one request from 0 (a refused tool call makes
    the next round). `response` is the provider's raw message, None when the
    call failed (then `error` is set). `duration_ms` is the wall time of the
    whole `invoke_response`, so it includes the transient-failure backoff, not only the provider's latency.
    """

    round: int
    response: Any | None
    duration_ms: float
    error: str | None


def _invoke_agent_shaped(
    llm: Any,
    messages: list[Any],
    *,
    tools: Sequence[Any],
    desc: str,
    model: str,
    retry_attempts: int,
    params: GenParams,
    on_call: Callable[[ModelCall], None] | None = None,
) -> str:
    """Send a finished agent-shaped request through the tool-bound model; return its text.

    `on_call` sees every provider call, the failed one included, before the
    outcome is acted on (the chunk path keeps the raw record of each).

    A response carrying tool calls is refused with a ToolMessage error and
    re-invoked, up to `params.tool_rounds` refusal rounds; past that the call
    fails (never an unbounded loop). Shared by the node generation above and
    the chunk generation (`chunk_generate.py`), which builds its own message
    list.
    """
    from langchain_core.messages import ToolMessage

    bound = llm.bind_tools(list(tools))
    messages = list(messages)
    limit = max(0, params.tool_rounds)
    rounds = 0
    while True:
        started = time.monotonic()
        try:
            response = invoke_response(
                bound,
                messages,
                desc=desc,
                error_type=GenerateError,
                retry_attempts=retry_attempts,
                model=model,
                usage_source="hierarchy.generate",
            )
        except GenerateError as exc:
            if on_call is not None:
                on_call(ModelCall(rounds, None, (time.monotonic() - started) * 1_000, str(exc)))
            raise
        if on_call is not None:
            on_call(ModelCall(rounds, response, (time.monotonic() - started) * 1_000, None))
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        if not tool_calls:
            text = extract_text(response)
            if text:
                return text
            raise GenerateError(
                f"Model ({desc}) returned empty response (possible safety block). "
                f"response_metadata: {getattr(response, 'response_metadata', None)!r}"
            )
        if rounds >= limit:
            names = ", ".join(str(tc.get("name")) for tc in tool_calls)
            raise GenerateError(
                f"Model ({desc}) kept calling tools after {limit} refusal round(s) "
                f"({names}) — tools are unavailable in this call"
            )
        rounds += 1
        messages.append(response)
        messages.extend(
            ToolMessage(content=_TOOL_REFUSAL, tool_call_id=str(tc.get("id") or ""))
            for tc in tool_calls
        )
