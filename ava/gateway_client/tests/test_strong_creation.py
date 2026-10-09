"""Explicit strong creation never downgrades an uncertain intent."""

from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from ava.gateway_client import spawn, transport
from ava.gateway_client.tests.test_gateway_client import mock_client as mock_client
from base.agents import GatewayUnavailable
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, mock_client: MagicMock) -> MagicMock:
    client = mock_client

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
def test_removed_mode_never_submits(client: MagicMock, bad: object) -> None:
    with pytest.raises(TypeError, match="require_idempotency"):
        _spawn(require_idempotency=bad, idempotency_key="key")
    client.post.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "x" * 129, 123])
def test_strong_requires_valid_caller_key(client: MagicMock, key: object) -> None:
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        _spawn(idempotency_key=key)
    client.post.assert_not_called()


def test_fork_uses_fixed_keyed_admission(client: MagicMock) -> None:
    client.post.return_value = httpx.Response(
        201, json={"id": 42}, request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents")
    )
    assert (
        spawn(
            spawner="user", prompt=None, fork_from=1, prompt_source="user", idempotency_key="fork"
        )
        == 42
    )
    assert client.post.call_args.args == ("/api/keyed/v1/agents",)
    assert client.post.call_args.kwargs["json"]["fork_from"] == 1
    assert client.post.call_args.kwargs["headers"][SCOPE_HEADER] == PRINCIPAL_SCOPE


def test_unknown_outcome_one_shot_and_caller_retry_preserves_path(client: MagicMock) -> None:
    path = "/api/keyed/v1/agents"
    client.post.side_effect = [
        httpx.ReadTimeout("lost"),
        httpx.Response(
            201, json={"id": 42}, request=httpx.Request("POST", "http://gateway" + path)
        ),
    ]
    with pytest.raises(GatewayUnavailable):
        _spawn(idempotency_key="intent")
    assert client.post.call_count == 1
    assert _spawn(idempotency_key="intent") == 42
    first, second = client.post.call_args_list
    assert first == second
    assert first.args == (path,)
    assert first.kwargs["headers"]["Idempotency-Key"] == "intent"
    assert first.kwargs["headers"][SCOPE_HEADER] == PRINCIPAL_SCOPE


@pytest.mark.parametrize("status", [404, 405, 422, 409, 500])
def test_guarded_http_error_never_falls_back(client: MagicMock, status: int) -> None:
    client.post.return_value = httpx.Response(
        status,
        json={"detail": "rejected"},
        request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        _spawn(idempotency_key="intent")
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
    assert _spawn(idempotency_key="intent") == 42
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


class TestSpawn:
    def test_spawn_returns_agent_id(self, mock_client: MagicMock):
        from ava.gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 42}
        mock_client.post.return_value = mock_resp

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key=str(uuid4()),
        )
        assert agent_id == 42

    def test_spawn_without_prompt(self, mock_client: MagicMock):
        from ava.gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 7}
        mock_client.post.return_value = mock_resp

        agent_id = spawn(
            spawner="agent:1",
            prompt=None,
            fork_from=5,
            prompt_source="agent",
            idempotency_key=str(uuid4()),
        )
        assert agent_id == 7

    def test_spawn_read_timeout_is_not_retried(self, mock_client: MagicMock):
        """Spawn is non-idempotent: a ReadTimeout means the gateway may have
        already created the agent (response lost, not request lost). Retrying
        the POST could spawn a phantom-twin agent, so the first read timeout
        must raise immediately — one POST, no re-send (task #698 G7)."""
        from ava.gateway_client import GatewayUnavailable, spawn

        mock_client.post.side_effect = httpx.ReadTimeout("gateway slow")

        with pytest.raises(GatewayUnavailable, match="no retry: non-idempotent"):
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key=str(uuid4()),
            )
        assert mock_client.post.call_count == 1

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("refused"),
            httpx.ConnectTimeout("dial stalled"),
            httpx.PoolTimeout("pool busy"),
        ],
    )
    @pytest.mark.usefixtures("retry_waits")
    def test_spawn_pre_send_error_is_retried(
        self, mock_client: MagicMock, error: httpx.TransportError
    ):
        """Connect-family failures happen before the request reaches the
        server, so re-sending a spawn is safe — the retry stays."""
        from ava.gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 42}
        mock_client.post.side_effect = [error, mock_resp]

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key=str(uuid4()),
        )
        assert agent_id == 42
        assert mock_client.post.call_count == 2
        keys = [
            call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list
        ]
        assert keys[0] == keys[1] and keys[0]

    @pytest.mark.usefixtures("retry_waits")
    def test_spawn_read_timeout_after_connect_error_retries_connect_only(
        self, mock_client: MagicMock
    ):
        """Mixed failure: the first connect error is retried, but the read
        timeout that follows is terminal — the request may have landed."""
        from ava.gateway_client import GatewayUnavailable, spawn

        mock_client.post.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("gateway slow"),
        ]

        with pytest.raises(GatewayUnavailable, match="no retry: non-idempotent"):
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key=str(uuid4()),
            )
        assert mock_client.post.call_count == 2


# --- send_message ---
