"""Exercise the real DeepSeek factory and SDKs against captured HTTP requests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx2 as httpx
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from base.lm.factory import build_chat_model


class Replies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: list[str]


def assert_native_request(
    request: httpx.Request, effort: ReasoningEffort, max_output_tokens: int | None
) -> None:
    body = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/responses"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "deepseek-flash"
    assert body["reasoning"] == {"effort": effort.value}
    assert body["max_output_tokens"] == max_output_tokens
    assert "tools" not in body
    output_format = body["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["required"] == ["suggestions"]
    assert output_format["schema"]["additionalProperties"] is False
    candidates = output_format["schema"]["properties"]["suggestions"]
    assert candidates["type"] == "array"
    assert candidates["items"]["type"] == "string"
    assert "maxItems" not in candidates


@pytest.fixture
async def captured_requests() -> AsyncIterator[tuple[list[httpx.Request], httpx.AsyncClient]]:
    requests: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "deepseek-flash",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"suggestions": ["Use A.", "Use B.", "Use C.", "Use D."]}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 4},
                    "output_tokens": 8,
                    "output_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 18,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        yield requests, client


@pytest.mark.parametrize("effort", [ReasoningEffort.NONE, ReasoningEffort.HIGH])
async def test_native_schema_output_uses_responses_and_preserves_model_settings_and_usage(
    model_catalog: ModelCatalog,
    effort: ReasoningEffort,
    captured_requests: tuple[list[httpx.Request], httpx.AsyncClient],
) -> None:
    requests, client = captured_requests
    model = build_chat_model(
        "deepseek-flash",
        reasoning_effort=effort,
        timeout=20,
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    result = await model.with_structured_output(
        Replies.model_json_schema(),
        method="json_schema",
        strict=True,
        include_raw=True,
        http_async_client=client,
    ).ainvoke("Choose A, B, C, or D.")

    (request,) = requests
    assert_native_request(request, effort, model_catalog.models["deepseek-flash"].max_output_tokens)
    assert isinstance(result, dict)
    assert result["parsed"] == {"suggestions": ["Use A.", "Use B.", "Use C.", "Use D."]}
    assert result["parsing_error"] is None
    raw = result["raw"]
    assert isinstance(raw, AIMessage)
    assert raw.usage_metadata is not None
    assert raw.usage_metadata.get("input_token_details", {}).get("cache_read") == 4
    assert raw.usage_metadata.get("output_token_details", {}).get("reasoning") == 2
    assert isinstance(model, ChatAnthropic)
    assert model.default_request_timeout == 20
    assert model.anthropic_api_url == "https://api.deepseek.com/anthropic"


def test_ordinary_chat_keeps_the_anthropic_transport(model_catalog: ModelCatalog) -> None:
    model = build_chat_model(
        "deepseek-flash",
        reasoning_effort=ReasoningEffort.NONE,
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    assert isinstance(model, ChatAnthropic)
    assert model.anthropic_api_url == "https://api.deepseek.com/anthropic"
    assert model.anthropic_api_key.get_secret_value() == "sk-test"
    assert model.thinking == {"type": "disabled"}
