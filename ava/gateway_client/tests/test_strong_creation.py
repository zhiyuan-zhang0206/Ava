"""Explicit strong creation never downgrades an uncertain intent."""

from unittest.mock import MagicMock

import httpx
import pytest

from ava.gateway_client import spawn, transport
from base.agents import GatewayUnavailable
from base.agents.context import AvaContext
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()

    def http_client(_context: AvaContext | None = None) -> MagicMock:
        return client

    monkeypatch.setattr(transport, "_http", http_client)

    def no_delay(*_args: object) -> int:
        return 0

    monkeypatch.setattr(transport, "_retry_delay_seconds", no_delay)
    return client


def _spawn(**kwargs: object) -> int:
    return spawn(
        spawner="user",
        prompt="goal",
        fork_from=None,
        prompt_source="user",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("bad", [1, "true", None, [True]])
def test_invalid_mode_never_submits(client: MagicMock, bad: object) -> None:
    with pytest.raises(TypeError, match="must be a bool"):
        _spawn(require_idempotency=bad, idempotency_key="key")
    client.post.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "x" * 129, 123])
def test_strong_requires_valid_caller_key(client: MagicMock, key: object) -> None:
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        _spawn(require_idempotency=True, idempotency_key=key)
    client.post.assert_not_called()


def test_strong_fork_rejected_before_http(client: MagicMock) -> None:
    with pytest.raises(ValueError, match="does not support fork_from"):
        spawn(
            spawner="user",
            prompt=None,
            fork_from=1,
            prompt_source="user",
            require_idempotency=True,
            idempotency_key="fork",
        )
    client.post.assert_not_called()


@pytest.mark.parametrize("strong", [False, True])
def test_unknown_outcome_one_shot_and_caller_retry_preserves_path(
    client: MagicMock, strong: bool
) -> None:
    path = "/api/keyed/v1/agents" if strong else "/api/agents"
    client.post.side_effect = [
        httpx.ReadTimeout("lost"),
        httpx.Response(
            201, json={"id": 42}, request=httpx.Request("POST", "http://gateway" + path)
        ),
    ]
    with pytest.raises(GatewayUnavailable):
        _spawn(require_idempotency=strong, idempotency_key="intent")
    assert client.post.call_count == 1
    assert _spawn(require_idempotency=strong, idempotency_key="intent") == 42
    first, second = client.post.call_args_list
    assert first == second
    assert first.args == (path,)
    assert first.kwargs["headers"]["Idempotency-Key"] == "intent"
    if strong:
        assert first.kwargs["headers"][SCOPE_HEADER] == PRINCIPAL_SCOPE
    else:
        assert SCOPE_HEADER not in first.kwargs["headers"]


@pytest.mark.parametrize("status", [404, 405, 422, 409, 500])
def test_guarded_http_error_never_falls_back(client: MagicMock, status: int) -> None:
    client.post.return_value = httpx.Response(
        status,
        json={"detail": "rejected"},
        request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        _spawn(require_idempotency=True, idempotency_key="intent")
    assert client.post.call_count == 1
    assert client.post.call_args.args == ("/api/keyed/v1/agents",)


def test_connect_retry_keeps_exact_guarded_intent(client: MagicMock) -> None:
    client.post.side_effect = [
        httpx.ConnectError("not sent"),
        httpx.Response(
            201,
            json={"id": 42},
            request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents"),
        ),
    ]
    assert _spawn(require_idempotency=True, idempotency_key="intent") == 42
    assert len(client.post.call_args_list) == 2
    assert client.post.call_args_list[0] == client.post.call_args_list[1]


@pytest.mark.parametrize(
    "scope,key,path",
    [
        ("legacy", "intent", "/api/keyed/v1/agents"),
        (PRINCIPAL_SCOPE, None, "/api/keyed/v1/agents"),
        (PRINCIPAL_SCOPE, "intent", "/api/agents/1/restart"),
    ],
)
def test_transport_scope_rejects_invalid_admission(
    client: MagicMock, scope: str, key: str | None, path: str
) -> None:
    with pytest.raises((ValueError, TypeError)):
        transport.post(path, idempotency_key=key, idempotency_scope=scope)
    client.post.assert_not_called()
