"""The shared tail of model calls: build -> invoke -> flatten -> empty-check.

`ava.web.fetch`'s answer step and `ava.understand`'s text/media paths each
used to carry their own copy of "build the chat model, invoke it on a
HumanMessage, flatten the response, reject an empty answer with the right
error type". This module is that tail in one place: `extract_text` (pure
flattening), `invoke_text` (invoke + flatten + empty-check, wrapped in the
caller's error type), and `answer_text` (build at a reasoning effort, then
invoke) for the prompt-vs-material shape both entry points use.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from shared.lm._effort import ReasoningEffort
from shared.resilience import extract_retry_after, jittered


def _limiter_sync(provider: str | None) -> Any:
    """The concurrency-limiter context for one sync invoke (no-op when
    disabled or provider unknown). Imported lazily so hot paths that never
    enable the limiter pay nothing at import time."""
    from shared.lm._concurrency import get_limiter

    return get_limiter().sync(provider)


def extract_text(response: Any) -> str:
    """Flatten a model response's content to plain text.

    str passthrough, or join the `text` blocks of a content-list response;
    anything else (a non-text content shape) is stringified.
    """
    # langchain's content-block dicts are dict[Unknown, Unknown]; the cast
    # states the shape we handle and the runtime isinstance guards below are
    # the real checks (a content list may also carry bare str elements).
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # langchain's content blocks are dicts with partially-unknown declared
        # shape; the cast states the shape we handle and the runtime isinstance
        # guard below is the real check (a content list may also carry bare
        # str elements).
        blocks = cast(list[dict[str, Any]], content)
        texts: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
        return "\n".join(texts)
    return str(content)


def invoke_text(
    llm: Any,
    content: list[Any],
    *,
    desc: str,
    error_type: type[Exception],
    retry_attempts: int = 0,
    retry_delay_seconds: float = 2.0,
    retry_max_delay_seconds: float = 30.0,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """Invoke `llm` on a single HumanMessage of `content`, return flattened text.

    `desc` labels failure messages (e.g. `deepseek-v4-flash, text`, or
    `deepseek-v4-flash for <url>` on the fetch path). A failed invoke or an
    empty (safety-blocked) response raises `error_type` with the reason.

    `retry_attempts` bounds retries of TRANSIENT/UNKNOWN provider failures
    (rate limit, 5xx, connection/timeout — the same classes the agent's LLM
    node retries via its LangGraph RetryPolicy). PERMANENT failures (400/401/
    402/403/404/422) are deterministic and never retried. A timeout on the
    wall-clock budget is the caller's concern (ava.understand / ava.web.fetch
    bound the whole call at the batch layer); this function retries the
    provider error classes that a retry can actually flip.

    Retry spacing is exponential backoff with jitter: the base
    `retry_delay_seconds` doubles per attempt (`2 → 4 → 8 → …`) capped at
    `retry_max_delay_seconds`, plus a per-process deterministic phase and
    random ±1s (R2-D `shared/resilience.jittered`) so neither parallel
    workers nor separate fleet processes re-hit the provider in lockstep
    (thundering herd). When
    the provider's error carries a `Retry-After` header (or `retry-after-ms`)
    asking for longer than the backoff would wait, the header wins (capped at
    120s) — mirroring what the provider SDKs do internally, but at this layer
    so a header longer than the SDK's 60s cap is still respected. The
    provider SDKs already retry 429/5xx up to twice before this loop sees an
    exception, so this is the second-order retry.

    `provider` is the `shared/lm/factory.py` provider key (`deepseek` /
    `claude` / …) for the outbound concurrency limiter
    (`shared/lm/_concurrency.py`, disabled by default); `None` skips the
    limiter entirely.
    """
    from langchain_core.messages import HumanMessage

    from shared.lm.errors import ErrorClass, classify_error

    retry_attempts = max(0, retry_attempts)  # a negative budget must not empty the loop
    retry_max_delay_seconds = max(retry_delay_seconds, retry_max_delay_seconds)
    response: Any | None = None
    start_time_ns: int | None = None
    for attempt in range(retry_attempts + 1):
        try:
            with _limiter_sync(provider):
                started = time.monotonic()
                response = llm.invoke([HumanMessage(content=content)])
            start_time_ns = time.time_ns() - int((time.monotonic() - started) * 1_000_000_000)
            break
        except Exception as e:
            retryable = classify_error(e).error_class in (ErrorClass.TRANSIENT, ErrorClass.UNKNOWN)
            if attempt < retry_attempts and retryable:
                delay = min(retry_delay_seconds * (2**attempt), retry_max_delay_seconds)
                retry_after = extract_retry_after(e)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                # Per-process deterministic phase + random ±1s (R2-D
                # jittered) — de-phases retry waves across the fleet.
                time.sleep(jittered(delay))
                continue
            raise error_type(
                f"Model call ({desc}) failed after {attempt + 1} attempt(s): {type(e).__name__}: {e}"
            ) from e

    # Unreachable: every failed attempt raises inside the loop. The explicit
    # check is for the type checker (response is only assigned on the success
    # path).
    if response is None:
        raise error_type(f"Model call ({desc}) failed: no attempt produced a response")
    resolved_model = model or getattr(llm, "model_name", None)
    if isinstance(resolved_model, str) and resolved_model:
        from shared.lm.billing import emit_billing_from_message

        emit_billing_from_message(
            response,
            model=resolved_model,
            usage_kind="chat",
            start_time_ns=start_time_ns,
        )
    text = extract_text(response)
    if not text:
        raise error_type(
            f"Model ({desc}) returned empty response (possible safety block). "
            f"response_metadata: {getattr(response, 'response_metadata', None)!r}"
        )
    return text


def answer_text(
    prompt: str,
    material: str,
    *,
    model: str,
    effort: str | ReasoningEffort,
    error_type: type[Exception],
    desc: str,
    build_error: Callable[[str, Exception], Exception] | None = None,
    retry_attempts: int = 0,
    retry_delay_seconds: float = 2.0,
    retry_max_delay_seconds: float = 30.0,
    timeout: float | None = None,
) -> str:
    """Build `model` at `effort` and answer `prompt` against `material`.

    The shared tail of `ava.web.fetch`'s answer step and `ava.understand`'s
    text path: `build_chat_model(model, reasoning_effort=effort)` then
    `invoke_text`. A build failure (missing API key etc.) raises `error_type` —
    `build_error(model, e)` when given (caller-specific message), else
    `error_type(str(e))`. `retry_attempts` / `retry_delay_seconds` /
    `retry_max_delay_seconds` pass through to `invoke_text`'s
    transient-failure retry; `timeout` is the provider-client request timeout
    (None = provider SDK default). `provider` is derived from `model` for the
    concurrency limiter (None when the model prefix is unregistered — the
    limiter then passes through).
    """
    from shared.lm.factory import build_chat_model

    try:
        llm = build_chat_model(model, reasoning_effort=effort, timeout=timeout)
    except RuntimeError as e:
        if build_error is not None:
            raise build_error(model, e) from e
        raise error_type(str(e)) from e
    from shared.lm.factory import provider_key_of_model

    return invoke_text(
        llm,
        [
            {"type": "text", "text": material},
            {"type": "text", "text": prompt},
        ],
        desc=desc,
        error_type=error_type,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
        retry_max_delay_seconds=retry_max_delay_seconds,
        provider=provider_key_of_model(model),
        model=model,
    )
