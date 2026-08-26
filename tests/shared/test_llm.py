"""shared/lm/factory.py — per-model deepseek max_tokens dispatch + model context windows.

The prefix-dispatch / provider-class / override-resolution contract is covered
in tests/agent/test_llm_factory.py. This file covers the per-model pieces that
the deepseek tier split introduced: each registered deepseek model gets its own
max output cap (an unregistered one fails fast), and MODEL_CONTEXT_WINDOW
reports the right input ceiling for the frontend usage gauge.

No API is hit — only the constructed ChatAnthropic's max_tokens attribute and
the static model maps are asserted.
"""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from shared.config import settings
from shared.lm.factory import MODEL_CONTEXT_WINDOW, build_chat_model
from shared.lm.registry import MODEL_IDENTITY


class TestDeepseekMaxTokens:
    """Per-model output cap dispatch. v4-pro and v4-flash both 384K today (same
    1M context / 384K output), but the value is looked up per model name so a
    future deepseek model with a different cap drops into _DEEPSEEK_MAX_TOKENS
    without touching build_chat_model — and an unregistered one fails fast
    rather than borrowing a wrong cap and 400-ing the server."""

    def test_pro_max_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "deepseek_api_key", SecretStr("sk-test-deepseek"))
        llm = build_chat_model("deepseek-v4-pro")
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000

    def test_flash_max_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "deepseek_api_key", SecretStr("sk-test-deepseek"))
        llm = build_chat_model("deepseek-v4-flash")
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000

    def test_vision_exp_max_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The vision variant rides the same deepseek branch — ChatAnthropic on the
        anthropic-compatible endpoint (which speaks image blocks for this model per
        api-docs.deepseek.com/guides/vision) — with the same 384K output cap."""
        monkeypatch.setattr(settings.lm, "deepseek_api_key", SecretStr("sk-test-deepseek"))
        llm = build_chat_model("deepseek-v4-flash-vision-exp")
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000
        assert "deepseek.com" in str(llm.anthropic_api_url)

    def test_unknown_deepseek_model_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deepseek-prefixed model not in _DEEPSEEK_MAX_TOKENS raises rather
        than silently borrowing some other model's cap (fail-fast: an unknown
        cap is a registration bug, surface it at build time)."""
        monkeypatch.setattr(settings.lm, "deepseek_api_key", SecretStr("sk-test-deepseek"))
        with pytest.raises(ValueError, match="Unknown deepseek model") as exc_info:
            build_chat_model("deepseek-v4-nonexistent")

        message = str(exc_info.value)
        assert "Known deepseek models:" in message
        assert "deepseek-v4-flash" in message
        assert "deepseek-v4-flash-vision-exp" in message
        assert "deepseek-v4-pro" in message


class TestModelContextWindow:
    """Input-token ceilings reported by the token-usage endpoint. deepseek-v4-pro
    and deepseek-v4-flash are both 1M context — a frontend gauge computing
    occupancy% off a stale 128K would over-report usage by ~8x."""

    def test_deepseek_pro_is_one_million(self) -> None:
        assert MODEL_CONTEXT_WINDOW["deepseek-v4-pro"] == 1_000_000

    def test_deepseek_flash_is_one_million(self) -> None:
        assert MODEL_CONTEXT_WINDOW["deepseek-v4-flash"] == 1_000_000

    def test_deepseek_vision_exp_is_one_million(self) -> None:
        assert MODEL_CONTEXT_WINDOW["deepseek-v4-flash-vision-exp"] == 1_000_000

    def test_kimi_k3_is_1m(self) -> None:
        assert MODEL_CONTEXT_WINDOW["kimi-k3"] == 1_048_576

    def test_glm_5_2_is_1m(self) -> None:
        assert MODEL_CONTEXT_WINDOW["glm-5.2"] == 1_000_000

    def test_glm_5_3_is_1m(self) -> None:
        assert MODEL_CONTEXT_WINDOW["glm-5.3"] == 1_000_000


class TestModelIdentity:
    """Per-model identity note injected into the system prompt — a model
    without one wakes up believing it is whatever its training data says."""

    def test_kimi_k3_identity(self) -> None:
        assert MODEL_IDENTITY["kimi-k3"] == "You are running on Kimi K3 (Moonshot)."
