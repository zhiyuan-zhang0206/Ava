"""DeepSeek chat transport with native Responses structured output.

DeepSeek's Anthropic endpoint supports only `output_config.effort`, so JSON-schema
output must use its Responses endpoint. Ordinary chat and tools retain the
Anthropic transport; this selects a wire API for the same model, never a model.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models import LanguageModelInput
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic

DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_API_BASE = f"{DEEPSEEK_API_BASE}/anthropic"


class DeepSeekChatModel(ThinkingTokensChatAnthropic):
    """Keep Anthropic chat; use Responses for native JSON-schema output."""

    def with_structured_output(
        self,
        schema: dict[str, Any] | type[Any],
        *,
        include_raw: bool = False,
        method: Literal["function_calling", "json_schema"] = "function_calling",
        http_client: Any | None = None,
        http_async_client: Any | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, dict[str, Any] | BaseModel]:
        """Use native schema output with optional caller-owned HTTP clients."""
        if method != "json_schema":
            if http_client is not None or http_async_client is not None:
                raise ValueError("Custom HTTP clients require method='json_schema'")
            return super().with_structured_output(
                schema, include_raw=include_raw, method=method, **kwargs
            )

        from langchain_openai import ChatOpenAI

        effort = self.model_kwargs.get("extra_body", {}).get("output_config", {}).get("effort")
        if self.thinking is not None and self.thinking.get("type") == "disabled":
            effort = "none"
        model = ChatOpenAI(
            model=self.model,
            api_key=self.anthropic_api_key,
            base_url=DEEPSEEK_API_BASE,
            use_responses_api=True,
            reasoning_effort=effort,
            timeout=self.default_request_timeout,
            max_retries=self.max_retries,
            max_completion_tokens=self.max_tokens,
            temperature=self.temperature,
            http_client=http_client,
            http_async_client=http_async_client,
        )
        kwargs.setdefault("strict", True)
        return model.with_structured_output(
            schema, method="json_schema", include_raw=include_raw, **kwargs
        )
