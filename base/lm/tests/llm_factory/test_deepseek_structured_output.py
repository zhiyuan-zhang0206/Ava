"""Exercise the real DeepSeek factory and SDKs against captured HTTP requests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2 as httpx
import openai
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
    assert request.headers["authorization"] == "Bearer test-deepseek-key"
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
async def captured_requests(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "deepseek-flash",
                    "content": [{"type": "text", "text": "Ready."}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            )
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

    for sdk in (openai.AsyncOpenAI, anthropic.AsyncAnthropic):
        original = sdk.__init__

        def init(instance: Any, *args: Any, _original: Any = original, **kwargs: Any) -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(send))
            clients.append(client)
            kwargs["http_client"] = client
            _original(instance, *args, **kwargs)

        monkeypatch.setattr(sdk, "__init__", init)
    try:
        yield requests
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.parametrize("effort", [ReasoningEffort.NONE, ReasoningEffort.HIGH])
async def test_native_schema_output_uses_responses_and_preserves_model_settings_and_usage(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    effort: ReasoningEffort,
    captured_requests: list[httpx.Request],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    model = build_chat_model(
        "deepseek-flash",
        reasoning_effort=effort,
        timeout=20,
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    result = await model.with_structured_output(
        Replies.model_json_schema(), method="json_schema", strict=True, include_raw=True
    ).ainvoke("Choose A, B, C, or D.")

    (request,) = captured_requests
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


async def test_ordinary_chat_keeps_the_anthropic_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    captured_requests: list[httpx.Request],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    model = build_chat_model(
        "deepseek-flash",
        reasoning_effort=ReasoningEffort.NONE,
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins({}),
    )
    result = await model.ainvoke("Ready?")

    (request,) = captured_requests
    assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "test-deepseek-key"
    assert json.loads(request.content)["thinking"] == {"type": "disabled"}
    assert result.content == "Ready."
