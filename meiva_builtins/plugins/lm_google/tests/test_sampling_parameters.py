from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from base.lm.provider_api import BuildContext
from meiva_builtins.plugins.lm_google.provider import PROVIDER, chat_model_kwargs


@pytest.mark.parametrize("model", sorted(PROVIDER.models))
def test_request_has_no_sampling_parameters(model: str) -> None:
    ctx = BuildContext(
        model=model,
        spec=PROVIDER.models[model],
        thinking=None,
        resolved_effort="",
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
    )
    llm = ChatGoogleGenerativeAI(**chat_model_kwargs(ctx, api_key="test-not-a-real-key"))

    config = cast(Any, llm)._prepare_request([HumanMessage(content="hi")])["config"]

    assert llm.temperature is None
    assert config.temperature is None
    assert config.top_p is None
    assert config.top_k is None


@pytest.mark.parametrize("model", ("gemini-3.5-flash", "gemini-3.7-flash"))
def test_deprecated_flash_models_are_superseded_by_3_8_flash(model: str) -> None:
    spec = PROVIDER.models[model]
    assert spec.spawnable is True
    assert spec.superseded_by == "gemini-3.8-flash"
