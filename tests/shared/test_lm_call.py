"""`shared/lm/_call.py` unit tests — the shared model-call tail.

`extract_text` / `invoke_text` / `answer_text` are the one copy of the
"invoke -> flatten -> empty-check, wrapped in the caller's error type" tail
that `ava.web.fetch`'s answer step and `ava.understand`'s text/media paths
used to each inline.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from shared.lm._call import answer_text, extract_text, invoke_text
from shared.lm._effort import ReasoningEffort


class _FakeResponse:
    def __init__(self, content: Any) -> None:
        self.content = content
        self.response_metadata = {"usage": 1}


class _FakeLLM:
    def __init__(self, response: Any, *, model_name: str | None = None) -> None:
        self._response = response
        self.model_name = model_name
        self.messages: list[Any] = []

    def invoke(self, messages: list[Any]) -> Any:
        self.messages = messages
        return self._response


# ─── extract_text ─────────────────────────────────────────────────────────


def test_extract_text_str_passthrough() -> None:
    assert extract_text(_FakeResponse("hello")) == "hello"


def test_extract_text_joins_text_blocks_skipping_non_text() -> None:
    resp = _FakeResponse(
        [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
            {"type": "image", "text": "skip me"},
            {"type": "text", "text": ""},
        ]
    )
    assert extract_text(resp) == "a\nb"


def test_extract_text_stringifies_other_shapes() -> None:
    assert extract_text(_FakeResponse(123)) == "123"


# ─── invoke_text ──────────────────────────────────────────────────────────


def test_invoke_text_returns_flattened_text() -> None:
    llm = _FakeLLM(_FakeResponse("answer"))
    out = invoke_text(llm, [{"type": "text", "text": "q"}], desc="d", error_type=ValueError)
    assert out == "answer"
    assert llm.messages[0].content == [{"type": "text", "text": "q"}]


def test_invoke_text_emits_chat_billing_span_from_llm_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful shared text call records the priced provider usage it consumed.

    The regression this catches is ``ava.understand`` and ``ava.web.fetch``
    returning an answer without the one-call/one-billing-span ledger row.
    """
    from opentelemetry import trace as otel_trace

    from shared import trace as trace_mod

    class _Span:
        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

        def end(self) -> None:
            pass

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[_Span] = []

        def start_span(self, _name: str, *, start_time: int | None = None) -> _Span:
            span = _Span()
            self.spans.append(span)
            return span

    tracer = _Tracer()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    llm = _FakeLLM(
        AIMessage(
            content="answer",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 20},
            },
        ),
        model_name="deepseek-v4-pro",
    )

    assert (
        invoke_text(llm, [{"type": "text", "text": "q"}], desc="d", error_type=ValueError)
        == "answer"
    )

    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes["ava.billing.vendor"] == "deepseek"
    assert tracer.spans[0].attributes["ava.billing.usage_kind"] == "chat"
    assert tracer.spans[0].attributes["ava.billing.tokens_in"] == 100
    assert tracer.spans[0].attributes["ava.billing.cache_read_tokens"] == 20


def test_invoke_text_logs_priced_chat_usage_with_source(
    loguru_records: list[dict[str, Any]],
) -> None:
    """A successful auxiliary text call enters the durable usage ledger.

    Removing the usage emitter would leave ``ava.web.fetch`` and
    ``ava.understand`` invisible to per-agent cost accounting even though
    their billing spans still exist.
    """
    llm = _FakeLLM(
        AIMessage(
            content="answer",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 20},
                "output_token_details": {"reasoning": 4},
            },
        ),
        model_name="deepseek-v4-pro",
    )

    assert (
        invoke_text(
            llm,
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            usage_source="web.fetch",
        )
        == "answer"
    )

    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    extra = record["extra"]
    assert extra["calls"] == 1
    assert extra["in_total"] == 100
    assert extra["cache_read"] == 20
    assert extra["out_total"] == 10
    assert extra["reasoning"] == 4
    assert extra["model"] == "deepseek-v4-pro"
    assert extra["usage_kind"] == "chat"
    assert extra["source"] == "web.fetch"
    assert extra["transport_source"] == "system"
    assert extra["cost_usd"] > 0


def test_invoke_text_skips_usage_log_without_usage_metadata(
    loguru_records: list[dict[str, Any]],
) -> None:
    """A response without provider token counts must not masquerade as metered."""
    llm = _FakeLLM(_FakeResponse("answer"), model_name="deepseek-v4-pro")

    assert (
        invoke_text(llm, [{"type": "text", "text": "q"}], desc="d", error_type=ValueError)
        == "answer"
    )

    assert not [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]


def test_invoke_text_empty_response_raises_error_type() -> None:
    """An empty (safety-blocked) response raises the caller's error type, not
    a silent empty string."""
    llm = _FakeLLM(_FakeResponse(""))
    with pytest.raises(ValueError, match="empty response"):
        invoke_text(llm, [{"type": "text", "text": "q"}], desc="d", error_type=ValueError)


def test_invoke_text_upstream_error_wrapped_in_error_type(loguru_records) -> None:
    class _Boom:
        def invoke(self, messages: list[Any]) -> Any:
            exc = RuntimeError("rate limit exceeded")
            exc.status_code = 429  # type: ignore[attr-defined]
            raise exc

    with pytest.raises(KeyError, match="rate limit"):
        invoke_text(
            _Boom(),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=KeyError,
            model="deepseek-v4-pro",
        )
    events = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "llm_provider_error"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert events[-1]["extra"]["status"] == 429
    assert events[-1]["extra"]["vendor"] == "deepseek"


# ─── answer_text ──────────────────────────────────────────────────────────


def test_answer_text_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Builds the model at the given effort and answers prompt vs material."""
    captured: dict[str, Any] = {}

    def _fake_build(
        model: str, *, reasoning_effort: Any = None, timeout: float | None = None
    ) -> Any:
        captured["model"] = model
        captured["effort"] = reasoning_effort
        return _FakeLLM(_FakeResponse("answer"))

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _fake_build)
    out = answer_text(
        "the prompt",
        "the material",
        model="m1",
        effort=ReasoningEffort.HIGH,
        error_type=ValueError,
        desc="d",
    )
    assert out == "answer"
    assert captured == {"model": "m1", "effort": ReasoningEffort.HIGH}


def test_answer_text_build_failure_raises_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build failure (missing API key etc.) surfaces as the caller's error
    type with the reason — the fetch-path contract."""

    def _fail(model: str, *, reasoning_effort: Any = None, timeout: float | None = None) -> Any:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _fail)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        answer_text("p", "m", model="m1", effort="low", error_type=ValueError, desc="d")


def test_answer_text_build_failure_uses_build_error_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch path passes `build_error` to keep its "for <url>" wording."""
    seen: dict[str, Any] = {}

    def _fail(model: str, *, reasoning_effort: Any = None, timeout: float | None = None) -> Any:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _fail)

    def _wrap(model: str, e: Exception) -> Exception:
        seen["model"] = model
        return KeyError(f"Building model {model!r} for <url> failed: {e}")

    with pytest.raises(KeyError, match=r"Building model 'm1' for <url> failed"):
        answer_text(
            "p",
            "m",
            model="m1",
            effort="low",
            error_type=ValueError,
            desc="d",
            build_error=_wrap,
        )
    assert seen["model"] == "m1"


# ─── invoke_text retry ────────────────────────────────────────────────────


def test_invoke_text_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TRANSIENT provider failure (transport class) is retried when
    `retry_attempts` allows, and a later success returns normally."""
    import httpx

    monkeypatch.setattr("time.sleep", lambda _: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    calls = {"n": 0}

    class _Flaky:
        def invoke(self, messages: list[Any]) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom")
            return _FakeResponse("answer")

    out = invoke_text(
        _Flaky(),
        [{"type": "text", "text": "q"}],
        desc="d",
        error_type=ValueError,
        retry_attempts=2,
        retry_delay_seconds=0.0,
    )
    assert out == "answer"
    assert calls["n"] == 2


def test_invoke_text_retry_exhausted_raises_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure that persists past the retry budget raises the caller's
    error type with the attempt count in the message."""
    import httpx

    monkeypatch.setattr("time.sleep", lambda _: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    class _Always:
        def invoke(self, messages: list[Any]) -> Any:
            raise httpx.ConnectError("boom")

    with pytest.raises(ValueError, match="after 2 attempt"):
        invoke_text(
            _Always(),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=1,
            retry_delay_seconds=0.0,
        )


def test_invoke_text_retry_uses_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry waits double per attempt (base 2 → 4 → 8…) capped at
    retry_max_delay_seconds, plus jitter; the base itself is not re-added."""
    import httpx

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    class _Always:
        def invoke(self, messages: list[Any]) -> Any:
            raise httpx.ConnectError("boom")

    with pytest.raises(ValueError, match="after 4 attempt"):
        invoke_text(
            _Always(),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=3,
            retry_delay_seconds=2.0,
            retry_max_delay_seconds=30.0,
        )
    assert sleeps == [2.0, 4.0, 8.0]


def test_invoke_text_retry_backoff_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exponential sequence stops growing at retry_max_delay_seconds."""
    import httpx

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    class _Always:
        def invoke(self, messages: list[Any]) -> Any:
            raise httpx.ConnectError("boom")

    with pytest.raises(ValueError, match="after 5 attempt"):
        invoke_text(
            _Always(),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=4,
            retry_delay_seconds=2.0,
            retry_max_delay_seconds=5.0,
        )
    assert sleeps == [2.0, 4.0, 5.0, 5.0]


class _RetryAfterHeaders:
    def __init__(self, **values: str) -> None:
        self._v = {k.lower(): v for k, v in values.items()}

    def get(self, key: str) -> str | None:
        return self._v.get(key.lower())


class _RetryAfterLLM:
    """An llm whose invoke always raises a 429-shaped TRANSIENT error carrying
    a Retry-After response header — the provider-SDK error shape."""

    def __init__(self, header: tuple[str, str]) -> None:
        self._header = header

    def invoke(self, messages: list[Any]) -> Any:
        import httpx

        exc = httpx.ConnectError("429 rate limited")
        exc.response = type(  # type: ignore[attr-defined]
            "R", (), {"headers": _RetryAfterHeaders(**{self._header[0]: self._header[1]})}
        )()
        raise exc


def test_invoke_text_retry_respects_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider Retry-After header longer than the backoff wins (capped at
    120s), plus jitter — mirroring the SDKs' own header reading at our layer."""
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="429"):
        invoke_text(
            _RetryAfterLLM(("Retry-After", "45")),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=2,
            retry_delay_seconds=2.0,
        )
    # attempt 1 backoff would be 2s, but Retry-After 45 wins; attempt 2 too.
    assert sleeps == [45.0, 45.0]


def test_invoke_text_retry_after_capped_at_120(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Retry-After above 120s is ignored (treated as absent) — the backoff
    sequence applies instead."""
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="429"):
        invoke_text(
            _RetryAfterLLM(("Retry-After", "300")),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=2,
            retry_delay_seconds=2.0,
        )
    assert sleeps == [2.0, 4.0]  # Retry-After 300 ignored


def test_invoke_text_retry_after_ms_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry-after-ms` (the SDKs' non-standard precision header) is honored."""
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._agent_phase", lambda _span: 0.0)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="429"):
        invoke_text(
            _RetryAfterLLM(("retry-after-ms", "7000")),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=1,
            retry_delay_seconds=2.0,
        )
    assert sleeps == [7.0]  # 7000ms → 7s, longer than the 2s backoff


def test_invoke_text_does_not_retry_permanent() -> None:
    """A PERMANENT provider rejection (e.g. 401) is deterministic — never
    retried even with a retry budget."""
    calls = {"n": 0}

    class _Auth:
        def invoke(self, messages: list[Any]) -> Any:
            calls["n"] += 1
            exc = RuntimeError("401 unauthorized")
            exc.status_code = 401  # type: ignore[attr-defined]
            raise exc

    with pytest.raises(ValueError, match="401"):
        invoke_text(
            _Auth(),
            [{"type": "text", "text": "q"}],
            desc="d",
            error_type=ValueError,
            retry_attempts=3,
            retry_delay_seconds=0.0,
        )
    assert calls["n"] == 1  # no retry on PERMANENT
