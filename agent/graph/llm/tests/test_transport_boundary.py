"""Only transport raised by model iteration is normalized for provider retry."""

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._stream import _consume_stream_with_stall_timeout
from base.lm.errors import is_retryable_provider_error


@pytest.mark.parametrize("during_model_iteration", [False, True])
async def test_transport_authority_stays_at_the_model_boundary(
    during_model_iteration: bool,
) -> None:
    error = httpx.ConnectError("connection interrupted")

    async def stream() -> AsyncIterator[AIMessage]:
        if during_model_iteration:
            raise error
        yield AIMessageChunk(content="one chunk")

    sink = MagicMock(spec=RedisStreamHandler)
    if not during_model_iteration:
        sink.process_chunk.side_effect = error
    handler = cast(RedisStreamHandler, sink)
    expected = ModelConnectionError if during_model_iteration else httpx.ConnectError
    with pytest.raises(expected) as raised:
        await _consume_stream_with_stall_timeout(
            stream(), chunks=[], handler=handler, ttft_timeout=1, inter_chunk_timeout=1
        )
    if during_model_iteration:
        assert raised.value.__cause__ is error
        assert is_retryable_provider_error(raised.value)
    else:
        assert raised.value is error
        assert not is_retryable_provider_error(error)
