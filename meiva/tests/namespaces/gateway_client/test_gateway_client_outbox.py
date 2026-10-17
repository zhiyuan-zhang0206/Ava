"""Bounded same-key message delivery, without a sender journal."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from base.api_contracts.agent_contract import GatewayUnavailable
from meiva.gateway_client import send_message, use_client


@pytest.fixture()
def mock_client() -> Iterator[MagicMock]:
    """Bind the HTTP client through the transport's public lifecycle seam."""
    client = MagicMock()
    with use_client(client):
        yield client


@pytest.mark.parametrize(
    "error",
    [
        httpx.WriteTimeout("write stalled"),
        httpx.ReadError("reply lost"),
        httpx.RemoteProtocolError("peer closed"),
    ],
)
def test_keyed_spawn_retries_uncertain_transport_error_under_one_key(
    mock_client: MagicMock, error: httpx.TransportError
) -> None:
    from meiva.gateway_client import spawn

    mock_client.post.side_effect = error
    with pytest.raises(GatewayUnavailable, match=r"after \d+ retries"):
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key=str(uuid4()),
        )
    assert mock_client.post.call_count > 1
    sent = {call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list}
    assert len(sent) == 1


def reply(status_code: int, **body: object) -> httpx.Response:
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", "http://gateway"))


def test_transient_send_retries_one_key_and_has_no_journal(
    mock_client: MagicMock, tmp_path: Path
) -> None:
    mock_client.post.side_effect = [reply(503), reply(201, status="enqueued")]
    send_message(7, content="hello", source="user")
    keys = [call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list]
    assert len(keys) == 2 and len(set(keys)) == 1
    from base.paths import meiva_home

    assert not (meiva_home() / "state" / "delivery-outbox").exists()


def test_separate_calls_keep_separate_logical_messages(mock_client: MagicMock) -> None:
    mock_client.post.return_value = reply(201, status="enqueued")
    send_message(7, content="hello", source="user")
    send_message(7, content="hello", source="user")
    keys = [call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list]
    assert len(set(keys)) == 2


def test_unknown_server_error_is_exposed_once(mock_client: MagicMock) -> None:
    mock_client.post.return_value = reply(500, detail="unknown")
    with pytest.raises(httpx.HTTPStatusError):
        send_message(7, content="hello", source="user")
    assert mock_client.post.call_count == 1


def test_permanent_failure_is_not_retried(mock_client: MagicMock) -> None:
    mock_client.post.return_value = reply(422, detail="invalid input")
    with pytest.raises(httpx.HTTPStatusError):
        send_message(7, content="hello", source="user")
    assert mock_client.post.call_count == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("reply lost"), httpx.ConnectError("offline")])
def test_message_transport_retry_exhausts_one_stable_key(
    mock_client: MagicMock, error: httpx.TransportError
) -> None:
    mock_client.post.side_effect = error
    with pytest.raises(GatewayUnavailable):
        send_message(7, content="hello", source="user")
    keys = [call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list]
    assert 1 < len(keys) <= 4 and len(set(keys)) == 1
