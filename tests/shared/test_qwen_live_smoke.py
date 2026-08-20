"""Live DashScope regression guard for the two facts that had no documentation.

Both were open risks when the provider shipped and both were then settled
empirically on 2026-08-20, against a dedicated Model Studio workspace endpoint,
for `qwen3.8-max` AND `qwen3.8-27b`:

1. **`enable_thinking: false` is honored** — HTTP 200, `reasoning_content`
   absent, content exactly the requested token. Not the 400 an undocumented
   switch risked. This matters because Ava's short-text paths (the labeler, the
   judge) send it on every call.
2. **The streamed terminal usage frame does carry
   `prompt_tokens_details.cached_tokens`** — measured `{"prompt_tokens": 2723,
   "prompt_tokens_details": {"cached_tokens": 2048, ...}}` on a warm repeat of a
   ~2.7k-token prefix, 0 on the cold first call (27b: 1664 warm). So the ledger
   reads a real number instead of silently billing every turn as a full cache
   miss. Both tests stream rather than invoke because that is the specific claim.

Neither is guaranteed to stay true across a vendor model revision, which is what
this file now guards. A third question remains genuinely open:

3. **Which field and string carry a billing / arrears error?** Provoking it needs
   an exhausted account. The error SHAPE is known, captured by pointing this file
   at a deliberately bogus key::

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

   `shared/lm/errors.py` now matches its billing vocabulary against BOTH
   `error.type` and `error.code`, so which of the two carries the reason no
   longer decides whether the alert can fire. What is still unconfirmed is the
   SPELLING: the vocabulary carries Alibaba's documented `Arrearage` plus the
   two overdue-bill codes, and Alibaba publishes those in a single table that
   never says which entries the compatible endpoint re-spells — its
   compatible-mode page documents exactly one code, the `invalid_api_key` above.
   No live arrears body has ever been seen to check the set against.

   Two ways to close that, in the order they are likely to happen. Whoever hits
   a real billing failure should **capture the raw body** and reconcile it with
   the vocabulary — the alert is silent either way, so nothing else will surface
   a mismatch. Or, deliberately: point `AVA_LIVE_DASHSCOPE_KEY` at a key on a
   drained account and run this file; the arrears rejection arrives on the first
   call. That needs an account with no balance, which is why the assertion lives
   in that operator's hands rather than in a test here.

## Running it

    AVA_LIVE_DASHSCOPE_KEY=sk-... .venv/bin/python -m pytest \
        tests/shared/test_qwen_live_smoke.py -v

Costs a few cents. Skipped whenever that variable is unset, which is every CI
run and every ordinary local run. A dedicated Model Studio workspace is not
reachable from the public default endpoint, so point the run at it with
`AVA_LIVE_DASHSCOPE_BASE_URL=https://<workspace-id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
— unset, the run uses whatever `AVA_DASHSCOPE_BASE_URL` resolves to.

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

# Both registered Qwen models — the vendor could revise either independently, and
# the two facts below were measured on both.
_MODELS = ("qwen3.8-max", "qwen3.8-27b")
_LIVE_KEY = os.environ.get("AVA_LIVE_DASHSCOPE_KEY")
_LIVE_BASE_URL = os.environ.get("AVA_LIVE_DASHSCOPE_BASE_URL")

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
    if _LIVE_BASE_URL:
        monkeypatch.setattr(settings.lm, "dashscope_base_url", _LIVE_BASE_URL)


def _stream(model: str, text: str, *, thinking_disabled: bool) -> AIMessage:
    """Stream one turn and accumulate it exactly as `agent/graph/_llm.py` does.

    Chunk accumulation followed by `message_chunk_to_message` is what preserves
    usage_metadata on the committed message — reproducing it here is the point,
    since fact 2 is specifically about the streamed terminal frame.
    """
    llm = build_chat_model(model, thinking={"type": "disabled"} if thinking_disabled else None)
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


@pytest.mark.parametrize("model", _MODELS)
def test_enable_thinking_false_actually_suppresses_reasoning(model: str) -> None:
    """Fact 1, asserted from both sides so a green cannot be vacuous.

    Checking only the disabled call would pass just as happily if this model
    never emitted reasoning at all, or if the parameter were silently ignored
    while the reasoning simply landed somewhere unread. So prove reasoning is
    observable with thinking ON first, then prove it disappears with it OFF.
    """
    thinking_on = _stream(model, "In two sentences: why is the sky blue?", thinking_disabled=False)
    assert _reasoning_text(thinking_on) or _reasoning_tokens(thinking_on) > 0, (
        f"{model}: no reasoning observed even with thinking ON — the rest of this "
        "test would be vacuous; check whether it still reasons by default"
    )

    off = _stream(model, "In two sentences: why is the sky blue?", thinking_disabled=True)
    assert _reasoning_text(off) == "", f"{model}: enable_thinking=false did not suppress reasoning"
    assert _reasoning_tokens(off) == 0, (
        f"{model}: enable_thinking=false hid the reasoning text but the model still "
        "billed reasoning tokens — the switch is cosmetic, not a cost lever"
    )
    assert str(message_content(off)).strip(), f"{model}: no answer text came back"


@pytest.mark.parametrize("model", _MODELS)
def test_streamed_usage_frame_carries_the_implicit_cache_hit(model: str) -> None:
    """Fact 2. Two turns over an identical long prefix: the second hits
    DashScope's implicit cache, which is always on and cannot be opted out of.

    The prefix has to clear the documented minimum (256 tokens for most models,
    ~1000 for the largest) — this one is comfortably over 1024. The cache TTL is
    documented only as "indeterminate", so a first-ever run can legitimately miss;
    re-run once before believing a failure.
    """
    prefix = "The quick brown fox jumps over the lazy dog near the river bank. " * 400

    first = _stream(model, f"{prefix}\n\nReply with exactly: ONE", thinking_disabled=True)
    assert (first.usage_metadata or {}).get("input_tokens", 0) > 1024, (
        f"{model}: prefix did not clear the minimum cacheable length"
    )

    second = _stream(model, f"{prefix}\n\nReply with exactly: TWO", thinking_disabled=True)
    details = (second.usage_metadata or {}).get("input_token_details") or {}
    assert "cache_read" in details, (
        f"{model}: the streamed terminal usage frame carried no cache_read — "
        "DashScope's prompt_tokens_details.cached_tokens does not survive the "
        "streaming path, so every turn is billing as a full cache miss"
    )
    assert int(details["cache_read"]) > 0, (
        f"{model}: cache_read present but zero on an identical repeated prefix — "
        "either the implicit cache did not hit (re-run once) or it is not reported"
    )
