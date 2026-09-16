"""Unit tests for the hierarchy generation pass (`shared/hierarchy/generate.py`).

Load-bearing contracts locked here:
- the budget is the one formula `min(source/10, hard cap)` over the measured
  input tokens, and the prompt asks below it;
- an over-budget response gets bounded compression, and a node still over
  budget after them fails with an error instead of producing an over-budget
  text;
- one node's provider failure never sinks the batch, and results keep input
  order under a bounded fan-out.

Every model call goes through a deterministic fake — no network, no provider.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from shared.hierarchy.generate import (
    GenParams,
    GenRequest,
    build_prompt,
    char_bounds,
    clean_text,
    generate_nodes,
)
from shared.hierarchy.seal import NARRATIVE_CAP_TOK, narrative_budget_tok
from shared.hierarchy.tokens import count_tokens

MODEL = "deepseek-v4-flash"

# A CJK filler keeps inputs comfortably above the per-node floor; exact token
# counts are still read back through `count_tokens` in the assertions, never
# assumed (the filler is 1-2 tokens/char depending on the run).
CJK = "\u5b57"


def filler(n: int) -> str:
    return CJK * n


def call_pair(call: list[BaseMessage]) -> tuple[str, str]:
    """The (material, prompt) text pair of one fake call."""
    content = call[0].content
    assert isinstance(content, list)
    first = cast("dict[str, Any]", content[0])
    second = cast("dict[str, Any]", content[1])
    return str(first["text"]), str(second["text"])


class FakeLLM:
    """Deterministic chat model: one responder over invoke calls, with
    in-flight accounting for the fan-out bound."""

    def __init__(self, responder: Callable[[list[BaseMessage]], str | Exception]) -> None:
        self.responder = responder
        self.calls: list[list[BaseMessage]] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.calls.append(messages)
        try:
            time.sleep(0.02)
            content = self.responder(messages)
        finally:
            with self._lock:
                self._in_flight -= 1
        if isinstance(content, Exception):
            raise content
        return AIMessage(content=content)


def leaf_req(nid: str, chars: int = 2000) -> GenRequest:
    return GenRequest(nid=nid, kind="leaf", input_text=filler(chars))


# ---- budget + prompt math (pure functions) ----


def test_narrative_budget_is_min_of_tenth_and_cap() -> None:
    assert narrative_budget_tok(999) == 99
    assert narrative_budget_tok(10) == 1
    assert narrative_budget_tok(9) == 0
    assert narrative_budget_tok(10**9) == NARRATIVE_CAP_TOK


def test_char_bounds_math() -> None:
    # 100k source -> budget 10k -> ask 8.5k tokens -> 11.9k chars (CJK inflation).
    assert char_bounds(100_000) == (7140, 11900)
    # Small inputs hit the comprehension floor instead of a tiny ask.
    assert char_bounds(1_000) == (180, 300)
    # The hard cap binds before the ask can run away.
    assert char_bounds(10**9) == (10710, 17850)


def test_char_bounds_scale_with_params() -> None:
    p = GenParams(ask_ratio=1.0, chars_per_token=1.0, min_ask_chars=10, lo_ratio=0.5)
    assert char_bounds(1_000, p) == (50, 100)


def test_build_prompt_carries_bounds_per_kind() -> None:
    leaf = build_prompt("leaf", 100_000)
    node = build_prompt("node", 100_000)
    for prompt in (leaf, node):
        assert "7140-11900" in prompt
        assert "11900 is a HARD LIMIT" in prompt
        assert "{lo}" not in prompt and "{hi}" not in prompt
    assert "one stretch of an agent's work log" in leaf
    assert "lower-level node summaries" in node


def test_build_prompt_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown node kind"):
        build_prompt("bogus", 1_000)


def test_clean_text_strips_label_and_whitespace() -> None:
    assert clean_text("  hello  ") == "hello"
    assert clean_text("\u6458\u8981\uff1a\u6b63\u6587") == "\u6b63\u6587"
    assert clean_text("\u6458\u8981: \u6b63\u6587") == "\u6b63\u6587"
    assert clean_text("\u6b63\u6587") == "\u6b63\u6587"


def test_count_tokens_is_deterministic_and_monotonic() -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello") >= 1
    assert count_tokens("hello world") >= count_tokens("hello")
    assert count_tokens(filler(50)) >= count_tokens(filler(10))
    assert count_tokens("word " * 100) == count_tokens("word " * 100)


# ---- generation flow ----


def test_generate_ok_under_budget_single_call() -> None:
    req = leaf_req("L1#1")
    fake = FakeLLM(lambda _messages: "ok")
    results = generate_nodes([req], model=MODEL, llm=fake, retry_attempts=0)
    (res,) = results
    assert res.ok
    assert res.text == "ok"
    assert res.out_tok == count_tokens("ok")
    assert res.budget_tok == min(count_tokens(req.input_text) // 10, NARRATIVE_CAP_TOK)
    assert len(fake.calls) == 1
    material, prompt = call_pair(fake.calls[0])
    assert material == req.input_text
    assert prompt == build_prompt("leaf", res.src_tok)


def test_tiny_input_fails_as_result_error_without_a_call() -> None:
    req = GenRequest(nid="L1#0", kind="leaf", input_text="hi")
    fake = FakeLLM(lambda _messages: "ok")
    (res,) = generate_nodes([req], model=MODEL, llm=fake)
    assert not res.ok
    assert res.error is not None and "budget floor" in res.error
    assert fake.calls == []


def test_over_budget_is_compressed_into_budget() -> None:
    req = leaf_req("L1#1")
    long_text = filler(1500)
    short_text = filler(30)

    def responder(messages: list[BaseMessage]) -> str:
        _, prompt = call_pair(messages)
        return short_text if "character limit" in prompt else long_text

    fake = FakeLLM(responder)
    (res,) = generate_nodes([req], model=MODEL, llm=fake, retry_attempts=0)
    assert res.ok
    assert res.text == short_text
    assert res.out_tok == count_tokens(short_text)
    assert res.out_tok is not None and res.out_tok <= res.budget_tok
    assert len(fake.calls) == 2
    # The compression call compresses the first response onward.
    material, _ = call_pair(fake.calls[1])
    assert material == long_text


def test_over_budget_after_retries_fails_and_writes_nothing() -> None:
    req = leaf_req("L1#1")
    fake = FakeLLM(lambda _messages: filler(1500))
    (res,) = generate_nodes([req], model=MODEL, llm=fake, retry_attempts=0)
    assert not res.ok
    assert res.text is None
    assert res.error is not None and "over budget" in res.error
    assert len(fake.calls) == 1 + GenParams().compress_attempts


def test_node_failure_isolated_in_batch() -> None:
    reqs = [leaf_req("L1#1"), leaf_req("L1#2"), leaf_req("L1#3")]
    reqs[1] = GenRequest(nid="L1#2", kind="leaf", input_text=filler(2000) + " POISON")

    def responder(messages: list[BaseMessage]) -> str | Exception:
        material, _ = call_pair(messages)
        return RuntimeError("provider exploded") if "POISON" in material else "ok"

    fake = FakeLLM(responder)
    results = generate_nodes(reqs, model=MODEL, llm=fake, retry_attempts=0)
    assert [r.nid for r in results] == ["L1#1", "L1#2", "L1#3"]
    assert results[0].ok and results[2].ok
    assert not results[1].ok
    assert results[1].error is not None and "provider exploded" in results[1].error


def test_bounded_fan_out_and_input_order() -> None:
    reqs = [leaf_req(f"L1#{i}") for i in range(1, 5)]
    fake = FakeLLM(lambda _messages: "ok")
    results = generate_nodes(reqs, model=MODEL, llm=fake, max_concurrent=2, retry_attempts=0)
    assert [r.nid for r in results] == ["L1#1", "L1#2", "L1#3", "L1#4"]
    assert fake.max_in_flight == 2


def test_generate_nodes_rejects_caller_bugs_before_any_call() -> None:
    fake = FakeLLM(lambda _messages: "ok")
    dup = [leaf_req("L1#1"), leaf_req("L1#1")]
    with pytest.raises(ValueError, match="duplicate nids"):
        generate_nodes(dup, model=MODEL, llm=fake)
    bogus = GenRequest(nid="L1#1", kind="bogus", input_text="x")
    with pytest.raises(ValueError, match="unknown kind"):
        generate_nodes([bogus], model=MODEL, llm=fake)
    with pytest.raises(ValueError, match="max_concurrent"):
        generate_nodes([leaf_req("L1#1")], model=MODEL, llm=fake, max_concurrent=0)
    assert fake.calls == []
    assert generate_nodes([], model=MODEL, llm=fake) == []
