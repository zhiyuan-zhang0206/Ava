"""`shared/lm/_concurrency.py` unit tests — per-provider LLM concurrency.

DeepSeek Pro is capped by default; explicit empty configuration remains an
operator escape hatch. These tests pin both the disabled pass-through and the
enabled cap semantics, plus the config parse fail-fast.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from shared.lm._concurrency import (
    LLMConcurrencyLimiter,
    known_provider_keys,
    parse_limits,
)
from shared.lm._plugin_providers import ensure_provider_plugins_loaded


@pytest.fixture(scope="module", autouse=True)
def _load_provider_plugins() -> None:
    ensure_provider_plugins_loaded()


def test_deepseek_concurrency_cap_is_enabled_by_default() -> None:
    """The default protects DeepSeek Pro's 500-slot account across 16 turns."""
    from shared.config.lm import LmSettings

    assert parse_limits(LmSettings().llm_max_concurrent) == {"deepseek": 31}


# ─── parse_limits ─────────────────────────────────────────────────────────


def test_parse_limits_empty() -> None:
    assert parse_limits("") == {}
    assert parse_limits(None) == {}  # type: ignore[arg-type]
    assert parse_limits("   ") == {}


def test_parse_limits_valid() -> None:
    assert parse_limits("deepseek:400,claude:200") == {
        "deepseek": 400,
        "claude": 200,
    }


def test_parse_limits_case_insensitive_and_whitespace() -> None:
    assert parse_limits(" DeepSeek: 400 , CLAUDE:2 ") == {"deepseek": 400, "claude": 2}


def test_parse_limits_unknown_provider_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        parse_limits("deepseek:400,openrouter:100")


def test_parse_limits_non_integer_limit_fails_fast() -> None:
    with pytest.raises(ValueError, match="not an integer"):
        parse_limits("deepseek:many")


def test_parse_limits_non_positive_limit_fails_fast() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        parse_limits("deepseek:0")
    with pytest.raises(ValueError, match="positive integer"):
        parse_limits("deepseek:-5")


def test_parse_limits_empty_segment_fails_fast() -> None:
    with pytest.raises(ValueError, match="empty segment"):
        parse_limits("deepseek:400,")


def test_parse_limits_missing_colon_fails_fast() -> None:
    with pytest.raises(ValueError, match="provider:limit"):
        parse_limits("deepseek400")


def test_known_provider_keys_derive_from_all_provider_prefixes() -> None:
    """The accepted keys derive from the merged provider-prefix map (minus
    any trailing dash) — single source, so a new provider is accepted by the
    limiter the moment the catalog knows it (audit 2026-08-08 P2: the old
    explicit list drifted in the one direction parse_limits cannot catch)."""
    from shared.lm.factory import provider_key_map

    assert known_provider_keys() == {p.rstrip("-") for p in provider_key_map()}
    # and the parse path accepts exactly those
    csv = ",".join(f"{k}:1" for k in sorted(known_provider_keys()))
    assert set(parse_limits(csv)) == known_provider_keys()


def test_dashless_prefix_keeps_its_last_letter() -> None:
    """`qwen` is the one prefix with no trailing dash (Alibaba versions inside
    the family name: qwen3.8-max), so the key derivation must strip a dash only
    where there is one — a blanket `[:-1]` would publish `qwe`, and a cap the
    operator wrote as `qwen:8` would then fail to parse."""
    from shared.lm.factory import provider_key_of_model

    assert "qwen" in known_provider_keys()
    assert "qwe" not in known_provider_keys()
    assert provider_key_of_model("qwen3.8-max") == "qwen"
    assert parse_limits("qwen:8") == {"qwen": 8}


# ─── disabled pass-through ────────────────────────────────────────────────


def test_disabled_sync_is_pass_through() -> None:
    limiter = LLMConcurrencyLimiter.from_csv("")
    entered = False
    with limiter.sync("deepseek"):
        entered = True
    assert entered
    assert not limiter.is_enabled


def test_disabled_async_is_pass_through() -> None:
    async def _run() -> bool:
        limiter = LLMConcurrencyLimiter.from_csv("")
        async with limiter.async_acquire("deepseek"):
            return True
        return False

    assert asyncio.run(_run())
    assert not LLMConcurrencyLimiter.from_csv("").is_enabled


def test_unknown_provider_sync_is_pass_through() -> None:
    limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 1})
    entered = False
    with limiter.sync("openrouter"):  # not configured → no cap
        entered = True
    assert entered


def test_none_provider_is_pass_through() -> None:
    limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 1})
    with limiter.sync(None):
        pass

    async def _run() -> None:
        async with limiter.async_acquire(None):
            pass

    asyncio.run(_run())


# ─── enabled sync cap ─────────────────────────────────────────────────────


def test_sync_cap_queues_second_holder() -> None:
    limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 1})
    order: list[str] = []

    def first() -> None:
        with limiter.sync("deepseek"):
            order.append("first-in")
            threading.Event().wait(0.2)  # hold the slot briefly
            order.append("first-out")

    t1 = threading.Thread(target=first)
    t1.start()
    # Wait until the first holder has the slot, then the second queues.
    while not order:
        pass
    with limiter.sync("deepseek"):
        order.append("second")
    t1.join()
    assert order == ["first-in", "first-out", "second"]


def test_sync_cap_separate_per_provider() -> None:
    """Providers have independent slots — deepseek at cap does not block claude."""
    limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 1, "claude": 1})
    with limiter.sync("deepseek"), limiter.sync("claude"):
        pass  # both held concurrently: no deadlock


# ─── enabled async cap ────────────────────────────────────────────────────


def test_async_cap_queues_second_holder() -> None:
    async def _run() -> list[str]:
        limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 1})
        order: list[str] = []

        async def first() -> None:
            async with limiter.async_acquire("deepseek"):
                order.append("first-in")
                await asyncio.sleep(0.05)
                order.append("first-out")

        async def second() -> None:
            async with limiter.async_acquire("deepseek"):
                order.append("second")

        await asyncio.gather(first(), second())
        return order

    order = asyncio.run(_run())
    assert order == ["first-in", "first-out", "second"]


def test_async_cap_allows_concurrency_up_to_limit() -> None:
    async def _run() -> int:
        limiter = LLMConcurrencyLimiter.from_mapping({"deepseek": 3})
        peak = {"n": 0}
        active = {"n": 0}

        async def worker() -> None:
            async with limiter.async_acquire("deepseek"):
                active["n"] += 1
                peak["n"] = max(peak["n"], active["n"])
                await asyncio.sleep(0.02)
                active["n"] -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        return peak["n"]

    assert asyncio.run(_run()) == 3


def test_limiter_limits_exposed() -> None:
    limiter = LLMConcurrencyLimiter.from_csv("deepseek:400,claude:200")
    assert limiter.limits == {"deepseek": 400, "claude": 200}
