"""Google keeps the ordinary prompt and reports implicit-cache usage."""

from typing import Any

import pytest
from google.genai import types
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.llm.invoke import ainvoke_tool_call
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model
from base.lm.usage import log_usage_from_message


async def test_google_tool_call_uses_complete_prefix_and_accounts_cache_reads(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    loguru_records: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    llm = build_chat_model(
        "gemini-3.8-flash",
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins(None),
        streaming=False,
    )
    assert isinstance(llm, ChatGoogleGenerativeAI)
    requests: list[dict[str, Any]] = []

    async def generate(**request: Any) -> types.GenerateContentResponse:
        requests.append(request)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="summary")]),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1000,
                candidates_token_count=100,
                total_token_count=1100,
                cached_content_token_count=200,
            ),
        )

    monkeypatch.setattr(llm.async_client.models, "generate_content", generate)
    response = await ainvoke_tool_call(
        llm, [SystemMessage(content="stable system head"), HumanMessage(content="history")]
    )
    assert response.text == "summary"
    assert len(requests) == 1
    config = requests[0]["config"]
    assert config.cached_content is None
    assert config.system_instruction.parts[0].text == "stable system head"
    assert config.tools[0].function_declarations[0].name == "execute_code"
    assert requests[0]["contents"][0].parts[0].text == "history"
    assert response.usage_metadata is not None
    assert response.usage_metadata.get("input_token_details", {}).get("cache_read") == 200

    log_usage_from_message(response, "gemini-3.8-flash", catalog=model_catalog)
    usage = next(r["extra"] for r in loguru_records if r["extra"].get("event") == "llm_usage")
    assert usage["cache_read"] == 200
    assert "cache_mechanism" not in usage and "cache_scope" not in usage


async def test_cache_403_has_no_application_recovery(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
) -> None:
    from google.genai.errors import ClientError
    from langchain_core.exceptions import ModelError

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    llm = build_chat_model(
        "gemini-3.8-flash",
        catalog=model_catalog,
        llm_override="",
        overrides=ModelOverrides.from_pins(None),
        streaming=False,
    )
    assert isinstance(llm, ChatGoogleGenerativeAI)
    calls = 0
    failure = ClientError(
        403, {"error": {"message": "CachedContent not found or permission denied"}}
    )

    async def reject(**_request: Any) -> types.GenerateContentResponse:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(llm.async_client.models, "generate_content", reject)
    with pytest.raises(ModelError) as caught:
        await ainvoke_tool_call(
            llm, [SystemMessage(content="head"), HumanMessage(content="history")]
        )
    assert caught.value.__cause__ is failure
    assert calls == 1
