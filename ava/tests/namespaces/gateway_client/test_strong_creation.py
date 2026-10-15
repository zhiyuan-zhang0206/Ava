"""Explicit strong creation never downgrades an uncertain intent."""

from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from ava.gateway_client import spawn
from ava.sdk_surface import process_context
from ava.tests.namespaces.gateway_client.test_gateway_client import mock_client as mock_client
from base.agents.context.gateway_client import transport
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER


@pytest.fixture
def client(mock_client: MagicMock, retry_waits: list[float]) -> MagicMock:
    return mock_client


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


def test_unknown_outcome_is_retried_with_the_same_path_and_key(client: MagicMock) -> None:
    path = "/api/keyed/v1/agents"
    client.post.side_effect = [
        httpx.ReadTimeout("lost"),
        httpx.Response(
            201, json={"id": 42}, request=httpx.Request("POST", "http://gateway" + path)
        ),
    ]
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


@pytest.mark.parametrize("key", ["", "x" * 129, 12])
def test_transport_rejects_an_invalid_key_before_the_network(
    client: MagicMock, key: object
) -> None:
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        transport.post(
            "/api/keyed/v1/agents",
            idempotency_key=key,  # type: ignore[arg-type]
            context=process_context.current_context(),
        )
    client.post.assert_not_called()


def test_transport_has_no_scope_override(client: MagicMock) -> None:
    with pytest.raises(TypeError):
        transport.post(
            "/api/keyed/v1/agents",
            idempotency_scope="legacy",  # type: ignore[call-arg]
            context=process_context.current_context(),
        )
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

    @pytest.mark.usefixtures("retry_waits")
    def test_spawn_read_timeout_is_retried_under_the_same_key(self, mock_client: MagicMock):
        """Spawn is keyed: a ReadTimeout may mean the gateway already created
        the agent, so the retry carries the same key and the gateway replays
        the original creation instead of spawning a twin."""
        from ava.gateway_client import spawn

        mock_client.post.side_effect = [
            httpx.ReadTimeout("gateway slow"),
            httpx.Response(
                201,
                json={"id": 7},
                request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents"),
            ),
        ]

        agent_id = spawn(
            spawner="user",
            prompt="hello",
            fork_from=None,
            prompt_source="user",
            idempotency_key="creation-key",
        )
        assert agent_id == 7
        keys = [
            call.kwargs["headers"]["Idempotency-Key"] for call in mock_client.post.call_args_list
        ]
        assert keys == ["creation-key", "creation-key"]

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
    def test_spawn_retries_a_read_timeout_after_a_connect_error(self, mock_client: MagicMock):
        from ava.gateway_client import spawn

        mock_client.post.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("gateway slow"),
            httpx.Response(
                201,
                json={"id": 7},
                request=httpx.Request("POST", "http://gateway/api/keyed/v1/agents"),
            ),
        ]

        assert (
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key=str(uuid4()),
            )
            == 7
        )
        assert mock_client.post.call_count == 3


# --- send_message ---
