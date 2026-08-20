"""The one live DashScope call that settles what the mock suite cannot.

Everything else about the Qwen provider is asserted against Alibaba's published
contract (`tests/agent/test_llm_factory.py`): request shaping, the
`enable_thinking` switch, the base URL, usage plumbing. Three facts, though, are
not documented anywhere and can only be observed against a real endpoint. They
all fall out of one session with a real key, which is why they live in one file:

1. **Is `enable_thinking: false` honored on `qwen3.8-max`?** The Qwen3.8 family
   is absent from Alibaba's deep-thinking table entirely; the open-weights
   sibling card documents the parameter, and the model page publishes distinct
   thinking / non-thinking input ceilings, which only makes sense if a
   non-thinking mode exists. Ava's short-text paths (the labeler, the judge)
   send it on every call, so a rejection would 400 them.
2. **Does the STREAMED terminal usage frame carry
   `prompt_tokens_details.cached_tokens`?** Alibaba documents the field for the
   compatible endpoint, and separately documents that `stream_options.include_usage`
   (which the provider sets) produces a terminal usage chunk — but never the
   intersection. If the field is absent from the streamed frame, every qwen turn
   silently bills as a full cache miss. This is why both tests stream rather than
   invoke: the non-streaming path answers a different question.
3. **Which field and string carry a billing / arrears error?** Still not
   assertable — provoking it needs an exhausted account — but the error SHAPE is
   now known, captured by pointing this file at a deliberately bogus key::

       401 {'error': {'message': 'Incorrect API key provided. ...',
                      'type': 'invalid_request_error',
                      'param': None,
                      'code': 'invalid_api_key'},
            'request_id': '...'}

   Two things follow, and both cut against what the compatible-mode endpoint was
   assumed to do. `error.type` IS present and is an ordinary OpenAI-style class
   (`invalid_request_error`), not a dotted-PascalCase DashScope code — those
   belong to the NATIVE DashScope API, not this one. And the specific,
   machine-readable reason lives in `error.code` (`invalid_api_key`, snake_case),
   with `type` carrying only the broad class.

   So `shared/lm/errors.py`, which matches a vocabulary against `error.type`,
   will always see the generic class here and never the specific reason: an
   arrears string added to that set cannot fire on DashScope no matter how it is
   spelled. Reading `error.code` is the change that would make it possible.
   Whoever hits a real billing failure should still **capture the raw body** —
   the one remaining unknown is which string `code` carries for arrears.

## Running it

    AVA_LIVE_DASHSCOPE_KEY=sk-... .venv/bin/python -m pytest \
        tests/shared/test_qwen_live_smoke.py -v

Costs a few cents. Skipped whenever that variable is unset, which is every CI
run and every ordinary local run.

**Why not gate on `DASHSCOPE_API_KEY` itself** — the obvious design, and it is a
trap that yields a test nobody can ever run. Two independent mechanisms close it:
`DASHSCOPE_API_KEY` is a cluster-scope alias, so `shared/dotenv_boot.py`'s
env-authority pass DELETES it from `os.environ` at import unless this unit's own
`.env` declares it; and the suite runs under a throwaway `$AVA_HOME` with no
`.env` at all, so no provider key ever reaches `settings.lm.*` in tests. A gate
reading `settings.lm.dashscope_api_key` is therefore permanently False. The
opt-in variable is deliberately NOT a Settings alias so it survives both, and the
fixture injects it with `monkeypatch.setattr`, which is the pattern
`scripts/lint_no_os_environ.py` Rule 2 prescribes for exactly this reason.
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    message_chunk_to_message,
)
from pydantic import SecretStr

from shared.config import settings
from shared.lm.content import content_blocks
from shared.lm.factory import build_chat_model
from shared.message_kwargs import message_addl_kwargs, message_content

_MODEL = "qwen3.8-max"
_LIVE_KEY = os.environ.get("AVA_LIVE_DASHSCOPE_KEY")

pytestmark = pytest.mark.skipif(
    not _LIVE_KEY,
    reason="live DashScope call — set AVA_LIVE_DASHSCOPE_KEY to opt in (never set in CI)",
)


@pytest.fixture(autouse=True)
def _live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the real provider at the opt-in key for the duration of the test.

    `setattr` on the settings singleton rather than `setenv`: the env is read
    once at import, so a late `setenv` would silently no-op (the trap
    `lint_no_os_environ.py` Rule 2 exists to catch). The override is also pinned
    empty — a stray `AVA_LLM_OVERRIDE` in the operator's shell would otherwise
    route this to a fake model and the test would "pass" without calling
    DashScope at all.
    """
    assert _LIVE_KEY is not None  # guaranteed by pytestmark; narrows for the type checker
    monkeypatch.setattr(settings.lm, "dashscope_api_key", SecretStr(_LIVE_KEY))
    monkeypatch.setattr(settings.lm, "llm_override", "")


def _stream(text: str, *, thinking_disabled: bool) -> AIMessage:
    """Stream one turn and accumulate it exactly as `agent/graph/_llm.py` does.

    Chunk accumulation followed by `message_chunk_to_message` is what preserves
    usage_metadata on the committed message — reproducing it here is the point,
    since fact 2 is specifically about the streamed terminal frame.
    """
    llm = build_chat_model(_MODEL, thinking={"type": "disabled"} if thinking_disabled else None)
    chunks = list(llm.stream([HumanMessage(text)]))
    assert chunks, "stream produced no chunks"
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk
    assert isinstance(merged, AIMessageChunk)
    final = message_chunk_to_message(merged)
    assert isinstance(final, AIMessage)
    return final


def _reasoning_text(msg: AIMessage) -> str:
    """Every shape this provider could surface reasoning through, concatenated.

    `ReasoningContentChatModel` folds the `reasoning_content` delta into canonical
    thinking blocks, but a raw passthrough would leave it in additional_kwargs —
    read both so "no reasoning" cannot be an artifact of looking in one place.
    """
    content = message_content(msg)
    blocks = content_blocks(content) if not isinstance(content, str) else []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str) or block.get("type") != "thinking":
            continue
        thinking: object = block.get("thinking", "")
        parts.append(str(thinking))
    raw: object = message_addl_kwargs(msg).get("reasoning_content") or ""
    parts.append(str(raw))
    return "".join(parts)


def _reasoning_tokens(msg: AIMessage) -> int:
    details = (msg.usage_metadata or {}).get("output_token_details") or {}
    return int(details.get("reasoning", 0))


def test_enable_thinking_false_actually_suppresses_reasoning() -> None:
    """Fact 1, asserted from both sides so a green cannot be vacuous.

    Checking only the disabled call would pass just as happily if this model
    never emitted reasoning at all, or if the parameter were silently ignored
    while the reasoning simply landed somewhere unread. So prove reasoning is
    observable with thinking ON first, then prove it disappears with it OFF.
    """
    thinking_on = _stream("In two sentences: why is the sky blue?", thinking_disabled=False)
    assert _reasoning_text(thinking_on) or _reasoning_tokens(thinking_on) > 0, (
        "no reasoning observed even with thinking ON — the rest of this test would "
        "be vacuous; check whether qwen3.8-max still reasons by default"
    )

    thinking_off = _stream("In two sentences: why is the sky blue?", thinking_disabled=True)
    assert _reasoning_text(thinking_off) == "", "enable_thinking=false did not suppress reasoning"
    assert _reasoning_tokens(thinking_off) == 0, (
        "enable_thinking=false hid the reasoning text but the model still billed "
        "reasoning tokens — the switch is cosmetic, not a cost lever"
    )
    assert str(message_content(thinking_off)).strip(), "no answer text came back"


def test_streamed_usage_frame_carries_the_implicit_cache_hit() -> None:
    """Fact 2. Two turns over an identical long prefix: the second should hit
    DashScope's implicit cache, which is always on and cannot be opted out of.

    The prefix has to clear the documented minimum (256 tokens for most models,
    ~1000 for the largest) — this one is comfortably over 1024. The cache TTL is
    documented only as "indeterminate", so a first-ever run can legitimately miss;
    re-run once before believing a failure.
    """
    prefix = "The quick brown fox jumps over the lazy dog near the river bank. " * 400

    first = _stream(f"{prefix}\n\nReply with exactly: ONE", thinking_disabled=True)
    assert (first.usage_metadata or {}).get("input_tokens", 0) > 1024, (
        "prefix did not clear the minimum cacheable length"
    )

    second = _stream(f"{prefix}\n\nReply with exactly: TWO", thinking_disabled=True)
    details = (second.usage_metadata or {}).get("input_token_details") or {}
    assert "cache_read" in details, (
        "the streamed terminal usage frame carried no cache_read — DashScope's "
        "prompt_tokens_details.cached_tokens does not survive the streaming path, "
        "so every qwen turn is billing as a full cache miss"
    )
    assert int(details["cache_read"]) > 0, (
        "cache_read present but zero on an identical repeated prefix — either the "
        "implicit cache did not hit (re-run once) or it is not reported here"
    )
