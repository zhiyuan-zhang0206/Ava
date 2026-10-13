"""Fast receipts survive the pinned Responses SDK's stream and nonstream paths."""

import pytest
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import (
    _construct_lc_result_from_responses_api,
    _convert_responses_chunk_to_generation_chunk,
)
from openai.types.responses import Response, ResponseCompletedEvent

from base.lm.catalog import ModelCatalog
from base.lm.pricing import tally_tokens
from base.lm.reasoning import extract_reasoning_tokens
from base.lm.usage import usage_model


@pytest.mark.parametrize("tier", ["fast", "priority", "default"])
@pytest.mark.parametrize("stream", [False, True])
def test_response_receipt_and_token_details(
    tier: str, stream: bool, *, model_catalog: ModelCatalog
) -> None:
    # SDK network parsing constructs responses without strict enum validation;
    # model_construct reproduces that boundary for the newer 'fast' receipt.
    response = Response.model_construct(
        id="resp_test",
        object="response",
        created_at=0,
        model="gpt-6.1-sol",
        status="completed",
        output=[],
        service_tier=tier,
        usage={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_tokens_details": {"cached_tokens": 60},
            "output_tokens_details": {"reasoning_tokens": 4},
        },
    )
    if stream:
        event = ResponseCompletedEvent.model_construct(
            type="response.completed", sequence_number=1, response=response
        )
        _, _, _, generation = _convert_responses_chunk_to_generation_chunk(event, 0, 0, 0)
        assert generation is not None
        message = generation.message
    else:
        message = _construct_lc_result_from_responses_api(response).generations[0].message
    assert isinstance(message, AIMessage)
    assert message.response_metadata["service_tier"] == tier
    expected = "gpt-6.1-sol" if tier == "default" else "gpt-6.1-sol-fast"
    assert usage_model(message, "gpt-6.1-sol-fast", catalog=model_catalog) == expected
    assert tally_tokens([message]) == (100, 10, 60)
    assert extract_reasoning_tokens(message.usage_metadata) == 4
