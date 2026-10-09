"""send_message × deferred-delivery outbox (task #3757) — record + key reuse.

A known transient send failure leaves a durable record and raises;
a retry chain must share one idempotency key while undelivered so the record
and the eventual delivery dedup against each other; a permanent (4xx) failure
must not be journaled; unknown failures are exposed without automatic replay.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from base.agents import AgentNotFound, GatewayUnavailable
from base.agents.messages import delivery_outbox as outbox


def _client_mock() -> MagicMock:
    """A gateway client double: the transport's `_http()` returns it."""
    client = MagicMock()
    client.return_value = client
    return client


@pytest.mark.parametrize(
    "error",
    [
        httpx.WriteTimeout("write stalled"),
        httpx.ReadError("reply lost"),
        httpx.RemoteProtocolError("peer closed"),
    ],
)
@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_keyed_spawn_does_not_retry_uncertain_transport_error(
    mock_client: MagicMock, error: httpx.TransportError
) -> None:
    from ava.gateway_client import spawn

    mock_client.post.side_effect = error
    with pytest.raises(GatewayUnavailable, match="result unknown"):
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key=str(uuid4()),
        )
    assert mock_client.post.call_count == 1


def _limits(**overrides: object) -> outbox.DeliveryOutboxLimits:
    base: dict[str, object] = {
        "enabled": True,
        "retry_backoff_steps": (30.0, 60.0, 300.0, 900.0),
        "budget_seconds": 43200.0,
        "abandoned_retention_days": 30,
        "dedup_window_seconds": 900.0,
        "flush_interval_seconds": 30.0,
        "max_entries": 128,
    }
    base.update(overrides)
    return outbox.DeliveryOutboxLimits(**base)  # type: ignore[arg-type]


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(outbox, "limits", _limits)
    outbox._reset_caches_for_tests()
    yield tmp_path
    outbox._reset_caches_for_tests()


def _ok_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 201
    resp.is_success = True
    return resp


def _keys(mock_client: MagicMock) -> list[str]:
    return [call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list]


def _records(journal: Path) -> list[outbox.OutboxEntry]:
    directory = journal / "state" / "delivery-outbox"
    if not directory.is_dir():
        return []
    return [
        entry
        for path in sorted(directory.glob("*.json"))
        if (entry := outbox._read(path)) is not None
    ]


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_failed_send_records_with_the_key_it_used(mock_client: MagicMock, journal: Path) -> None:
    from ava.gateway_client import send_message
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(7, owns_loop=False)

    mock_client.post.side_effect = httpx.ConnectError("refused")
    with pytest.raises(GatewayUnavailable):
        send_message(42, content="hello", source="watcher:7")
    keys = set(_keys(mock_client))
    assert len(keys) == 1  # a final failure still shares one key
    records = _records(journal)
    assert len(records) == 1
    entry = records[0]
    assert entry.agent_id == 42 and entry.source == "watcher:7"
    assert entry.origin_agent_id == 7
    assert entry.content == "hello" and entry.client_message_id == keys.pop()
    assert entry.state == "pending" and entry.attempts == 1


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_retry_chain_shares_one_key_and_success_retires_the_record(
    mock_client: MagicMock, journal: Path
) -> None:
    from ava.gateway_client import send_message

    mock_client.post.side_effect = httpx.ConnectError("refused")
    with pytest.raises(GatewayUnavailable):
        send_message(42, content="hello", source="watcher:7")
    first_key = _keys(mock_client)[0]
    assert len(_records(journal)) == 1

    # The caller retries once the gateway is back: same logical message, same
    # key, and the pending record retires.
    mock_client.reset_mock()
    mock_client.post.side_effect = None
    mock_client.post.return_value = _ok_response()
    send_message(42, content="hello", source="watcher:7")
    assert _keys(mock_client) == [first_key]
    assert _records(journal) == []

    # The NEXT identical message is a new logical message with a fresh key.
    mock_client.reset_mock()
    mock_client.post.return_value = _ok_response()
    send_message(42, content="hello", source="watcher:7")
    assert _keys(mock_client) != [first_key]


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_permanent_wire_failure_is_not_recorded(mock_client: MagicMock, journal: Path) -> None:
    from ava.gateway_client import send_message

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 404
    resp.is_success = False
    resp.json.return_value = {"reason": "agent_not_found", "detail": "agent 42 not found"}
    mock_client.post.return_value = resp

    with pytest.raises(AgentNotFound):
        send_message(42, content="hello", source="watcher:7")
    assert _records(journal) == []


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_unknown_key_error_stops_before_sending(
    mock_client: MagicMock, journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava.gateway_client import send_message

    def _boom(**_kwargs: object) -> str:
        raise RuntimeError("outbox on fire")

    monkeypatch.setattr(outbox, "logical_key", _boom)
    mock_client.post.side_effect = httpx.ConnectError("refused")
    with pytest.raises(RuntimeError, match="outbox on fire"):
        send_message(42, content="hello", source="watcher:7")
    mock_client.post.assert_not_called()
    assert _records(journal) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {"detail": "bug"}),
        (500, {"retryable": False, "committed": True, "inbound_id": 73}),
        (503, {"retryable": False, "detail": "bug"}),
        (503, {"committed": True, "inbound_id": 73}),
    ],
)
def test_unknown_or_committed_response_is_exposed_once_without_outbox(
    journal: Path, status: int, body: dict[str, object]
) -> None:
    from ava.gateway_client import send_message
    from ava.gateway_client.transport import use_client

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, json={**body, "idempotency_key": request.headers["Idempotency-Key"]}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        use_client(client),
        pytest.raises(httpx.HTTPStatusError) as raised,
    ):
        send_message(42, content="hello", source="watcher:7")
    assert len(requests) == 1
    assert raised.value.response.json() == {
        **body,
        "idempotency_key": requests[0].headers["Idempotency-Key"],
    }
    assert _records(journal) == []


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_known_transient_response_retries_with_one_key(journal: Path, status: int) -> None:
    from ava.gateway_client import send_message
    from ava.gateway_client.transport import use_client

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status if len(requests) == 1 else 201)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        use_client(client),
    ):
        send_message(42, content="hello", source="watcher:7")
    assert len(requests) == 2
    assert len({request.headers["Idempotency-Key"] for request in requests}) == 1
    assert _records(journal) == []


@pytest.mark.parametrize(
    "error_type", [httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.DecodingError]
)
def test_unknown_transport_error_is_exposed_once_without_journal(
    journal: Path, error_type: type[httpx.TransportError]
) -> None:
    from ava.gateway_client import send_message
    from ava.gateway_client.transport import use_client

    attempts: list[httpx.Request] = []
    error = error_type("transport implementation bug")

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        use_client(client),
        pytest.raises(error_type) as raised,
    ):
        send_message(42, content="hello", source="watcher:7")
    assert raised.value is error
    assert len(attempts) == 1
    assert _records(journal) == []


@pytest.mark.parametrize("field", ["committed", "retryable"])
def test_invalid_retry_control_is_exposed_once(journal: Path, field: str) -> None:
    from ava.gateway_client import send_message
    from ava.gateway_client.transport import use_client

    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(503, json={field: "false"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        use_client(client),
        pytest.raises(ValueError, match=f"{field} must be a boolean"),
    ):
        send_message(42, content="hello", source="watcher:7")
    assert len(attempts) == 1
    assert _records(journal) == []


@pytest.mark.parametrize("committed", [False, True])
def test_terminal_response_stops_existing_outbox_but_keeps_explicit_retry_key(
    journal: Path, committed: bool
) -> None:
    from ava.gateway_client import send_message
    from ava.gateway_client.transport import use_client

    requests: list[httpx.Request] = []
    stage = "network_failure"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if stage == "network_failure":
            raise httpx.ConnectError("refused", request=request)
        if stage == "terminal_response":
            return httpx.Response(
                500, json={"committed": committed, "retryable": False, "inbound_id": 73}
            )
        return httpx.Response(201)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        use_client(client),
    ):
        with pytest.raises(GatewayUnavailable):
            send_message(42, content="hello", source="watcher:7")
        assert len(_records(journal)) == 1
        key = requests[0].headers["Idempotency-Key"]
        stage = "terminal_response"
        with pytest.raises(httpx.HTTPStatusError) as raised:
            send_message(42, content="hello", source="watcher:7")
        assert raised.value.response.json()["committed"] is committed
        assert _records(journal) == []
        stage = "explicit_retry"
        send_message(42, content="hello", source="watcher:7")
    assert {request.headers["Idempotency-Key"] for request in requests} == {key}


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_disabled_outbox_records_nothing_and_never_reuses_keys(
    mock_client: MagicMock, journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava.gateway_client import send_message

    monkeypatch.setattr(outbox, "limits", lambda: _limits(enabled=False))
    outbox._reset_caches_for_tests()
    mock_client.post.side_effect = httpx.ConnectError("refused")
    with pytest.raises(GatewayUnavailable):
        send_message(42, content="hello", source="watcher:7")
    first_key = _keys(mock_client)[0]
    mock_client.reset_mock()
    mock_client.post.side_effect = httpx.ConnectError("refused")
    with pytest.raises(GatewayUnavailable):
        send_message(42, content="hello", source="watcher:7")
    assert _keys(mock_client)[0] != first_key
    assert _records(journal) == []


@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_creation_explicit_key_survives_lost_response_and_caller_retry(
    mock_client: MagicMock,
) -> None:
    from ava.gateway_client import spawn

    response = httpx.Response(
        201, json={"id": 42}, request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents")
    )
    mock_client.post.side_effect = [httpx.ReadTimeout("reply lost"), response, response]
    with pytest.raises(GatewayUnavailable):
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation-a",
        )
    assert mock_client.post.call_count == 1
    assert (
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation-a",
        )
        == 42
    )
    assert (
        spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation-b",
        )
        == 42
    )
    assert _keys(mock_client) == ["creation-a", "creation-a", "creation-b"]
    assert all(
        call.kwargs["headers"]["Idempotency-Scope"] == "principal-v1"
        for call in mock_client.post.call_args_list
    )


@pytest.mark.parametrize("key", ["", "x" * 129, 12, ("key",)])
@patch("ava.gateway_client.transport._http", new_callable=_client_mock)
def test_creation_rejects_malformed_key_before_http(mock_client: MagicMock, key: object) -> None:
    from ava.gateway_client import spawn

    with pytest.raises((TypeError, ValueError), match="idempotency key"):
        spawn(
            spawner="user",
            prompt=None,
            fork_from=None,
            prompt_source="user",
            idempotency_key=key,  # pyright: ignore[reportArgumentType] — invalid runtime input
        )
    mock_client.post.assert_not_called()
