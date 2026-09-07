"""ava/_gateway_client.py tests.

Covers:
- _raise_from_response: wire contract error reconstruction
- _post / _get: retry logic + network-layer errors → GatewayUnavailable
- spawn / send_message: public API happy + error paths
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from shared.agents import GatewayUnavailable

# --- _raise_from_response ---


class TestRaiseFromResponse:
    def test_success_does_not_raise(self):
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = True
        _raise_from_response(resp)  # no raise

    def test_wire_json_error_reconstructed(self):
        """HTTP 400 + body {"reason":"agent_not_found","detail":"..."} → AgentNotFound."""
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 404
        resp.json.return_value = {"reason": "agent_not_found", "detail": "agent 99 not found"}

        from shared.agents import AgentNotFound

        with pytest.raises(AgentNotFound, match="agent 99 not found"):
            _raise_from_response(resp)

    def test_non_json_body_falls_through_to_http_error(self):
        """body is not JSON → raise_for_status raises HTTPStatusError."""
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 500
        resp.json.side_effect = json.JSONDecodeError("msg", "", 0)
        # Mock raise_for_status to actually raise
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            _raise_from_response(resp)

    def test_corrupted_content_encoding_falls_through(self):
        """A response body whose Content-Encoding fails to decode raises
        httpx.DecodingError (0.28.1, broken gzip/br stream) — same
        protocol-mismatch class as non-JSON: falls through to the clean
        HTTPStatusError instead of the DecodingError masking the status.

        Regression: _wire_reason only caught JSONDecodeError (task #1669).
        """
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 502
        resp.json.side_effect = httpx.DecodingError("corrupted gzip stream")
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            _raise_from_response(resp)
        # The status was surfaced as the primary error — the DecodingError
        # never escapes as the exception seen by the caller.
        assert excinfo.value.response.status_code == 502
        assert not isinstance(excinfo.value.__context__, httpx.DecodingError)

    def test_missing_reason_field_falls_through(self):
        """JSON present but missing reason field → raise_for_status."""
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 400
        resp.json.return_value = {"detail": "something"}
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            _raise_from_response(resp)

    def test_invalid_reason_value_falls_through(self):
        """reason value not in ErrorReason enum → ValueError → raise_for_status."""
        from ava._gateway_transport import _raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 400
        resp.json.return_value = {"reason": "garbage_value", "detail": "x"}
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            _raise_from_response(resp)

    def test_valid_reason_missing_detail_falls_through(self):
        """Valid `reason` but `detail` field missing → HTTPStatusError, not
        KeyError: 'detail' masking the status code (same class as task #1205).

        Regression: the reverse-lookup raise indexed `body["detail"]` without
        a guard, so a reason-bearing body with no detail leaked a raw KeyError
        and the HTTP status code never reached the caller.
        """
        from ava._gateway_transport import _raise_from_response

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        resp = httpx.Response(404, json={"reason": "agent_not_found"}, request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            _raise_from_response(resp)
        assert excinfo.value.response.status_code == 404
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_valid_reason_non_string_detail_falls_through(self):
        """Valid `reason` but non-string `detail` → HTTPStatusError with the
        status code; a malformed detail is a protocol mismatch, not an
        application error to reconstruct."""
        from ava._gateway_transport import _raise_from_response

        request = httpx.Request("GET", "http://gw/api/agents/7")
        resp = httpx.Response(
            404, json={"reason": "agent_not_found", "detail": {"nested": True}}, request=request
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            _raise_from_response(resp)
        assert excinfo.value.response.status_code == 404
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_503_without_reason_raises_http_error_with_status(self):
        """A real 503 with a JSON body lacking `reason` raises HTTPStatusError
        carrying the status code — not a KeyError chained over it (task #1205).

        Regression: raise_for_status used to run inside the `except KeyError`
        handler, so the traceback led with `KeyError: 'reason'` (the gateway's
        FastAPI-default body has no wire `reason`) and the 503 status was
        buried in the exception chain instead of being the primary error.
        """
        from ava._gateway_transport import _raise_from_response

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        resp = httpx.Response(503, json={"detail": "gateway unavailable"}, request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            _raise_from_response(resp)
        assert excinfo.value.response.status_code == 503
        # The HTTP error is the primary exception: no parse KeyError chained
        # as __context__ masking the original status code.
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_non_object_json_body_falls_through(self):
        """JSON body that is not an object (no `reason` possible) → raise_for_status."""
        from ava._gateway_transport import _raise_from_response

        request = httpx.Request("GET", "http://gw/api/agents")
        resp = httpx.Response(503, json=["not", "an", "object"], request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            _raise_from_response(resp)
        assert excinfo.value.response.status_code == 503
        assert not isinstance(excinfo.value.__context__, KeyError)


# --- _post retry ---


class TestPostRetry:
    @patch("ava._gateway_transport._client")
    def test_first_attempt_succeeds(self, mock_client: MagicMock):
        from ava._gateway_transport import _post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp

        result = _post("/api/agents", {"key": "val"})
        assert result is mock_resp
        assert mock_client.post.call_count == 1

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_retries_on_transport_error(self, mock_time: MagicMock, mock_client: MagicMock):
        from ava._gateway_transport import _post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        # First two fail, third succeeds. POST /api/agents is a
        # NON_IDEMPOTENT spawn (read timeout must NOT retry — twin risk), so
        # this retry test uses an IDEMPOTENT lifecycle endpoint instead.
        mock_client.post.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timeout"),
            mock_resp,
        ]

        result = _post("/api/agents/42/terminate")
        assert result is mock_resp
        assert mock_client.post.call_count == 3
        assert mock_time.sleep.call_count == 2

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_all_retries_exhausted_raises_gateway_unavailable(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        from ava._gateway_transport import _post

        mock_client.post.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            _post("/api/agents")
        assert mock_client.post.call_count == 3

    @patch("ava._gateway_transport._client")
    def test_http_4xx_not_retried(self, mock_client: MagicMock):
        """HTTP 4xx is an application error, no retry."""
        from ava._gateway_transport import _post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.is_success = False
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"reason": "agent_not_found", "detail": "x"}
        mock_client.post.return_value = mock_resp

        # _post returns the response, _raise_from_response is called by the caller
        result = _post("/api/agents")
        assert result is mock_resp
        assert mock_client.post.call_count == 1  # no retry


# --- _post timeout contract ---


class TestPostTimeoutContract:
    """What `_post` hands httpx, not what it was asked for.

    httpx distinguishes three things a caller can mean by `timeout`: a value
    (use it), the `USE_CLIENT_DEFAULT` sentinel (fall back to the client's
    configured timeout), and `None` (**never** time out). Only the sentinel
    falls back, so these assert the argument httpx actually receives — asserting
    `_post`'s own default would have passed all along while every POST in the
    SDK ran unbounded and `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS` did nothing.
    """

    @patch("ava._gateway_transport._client")
    def test_default_defers_to_client_timeout(self, mock_client: MagicMock):
        """No per-call timeout → httpx gets the sentinel, never None."""
        from ava._gateway_transport import _post

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        mock_client.post.return_value = ok

        _post("/api/memory/search", {"query": "x", "k": 5})

        passed = mock_client.post.call_args.kwargs["timeout"]
        assert passed is httpx.USE_CLIENT_DEFAULT
        assert passed is not None

    @patch("ava._gateway_transport._client")
    def test_explicit_timeout_is_forwarded(self, mock_client: MagicMock):
        """A per-call timeout still overrides the client default."""
        from ava._gateway_transport import _post

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        mock_client.post.return_value = ok
        per_call = httpx.Timeout(120.0)

        _post("/api/agents/1/messages", {"content": "hi"}, timeout=per_call)

        assert mock_client.post.call_args.kwargs["timeout"] is per_call

    def test_client_singleton_carries_the_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The client default the sentinel defers to is the configured one.

        Without this, `USE_CLIENT_DEFAULT` could be deferring to httpx's own
        5s default rather than `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS`.
        """
        import ava._gateway_transport as gc
        from shared.config import settings

        monkeypatch.setattr(gc, "_client", None)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr(settings.gateway, "gateway_client_http_timeout_seconds", 20.0)  # pyright: ignore[reportUnknownMemberType]

        client = gc._client_singleton()  # pyright: ignore[reportUnknownMemberType]
        try:
            assert client.timeout.read == 20.0  # pyright: ignore[reportUnknownMemberType]
            assert client.timeout.connect == 20.0  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()  # pyright: ignore[reportUnknownMemberType]


# --- _get retry ---


class TestGetRetry:
    @patch("ava._gateway_transport._client")
    def test_first_attempt_succeeds(self, mock_client: MagicMock):
        from ava._gateway_transport import _get

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        result = _get("/api/agents/1")
        assert result is mock_resp
        assert mock_client.get.call_count == 1

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_all_retries_exhausted_raises_gateway_unavailable(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        from ava._gateway_transport import _get

        mock_client.get.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable):
            _get("/api/agents/1")
        assert mock_client.get.call_count == 3


# --- spawn ---


class TestSpawn:
    @patch("ava._gateway_transport._client")
    def test_spawn_returns_agent_id(self, mock_client: MagicMock):
        from ava._gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 42}
        mock_client.post.return_value = mock_resp

        agent_id = spawn(spawner="user", prompt="hello", fork_from=None, prompt_source="user")
        assert agent_id == 42

    @patch("ava._gateway_transport._client")
    def test_spawn_without_prompt(self, mock_client: MagicMock):
        from ava._gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 7}
        mock_client.post.return_value = mock_resp

        agent_id = spawn(spawner="agent:1", prompt=None, fork_from=5, prompt_source="agent")
        assert agent_id == 7

    @patch("ava._gateway_transport._client")
    def test_spawn_read_timeout_is_not_retried(self, mock_client: MagicMock):
        """Spawn is non-idempotent: a ReadTimeout means the gateway may have
        already created the agent (response lost, not request lost). Retrying
        the POST could spawn a phantom-twin agent, so the first read timeout
        must raise immediately — one POST, no re-send (task #698 G7)."""
        from ava._gateway_client import GatewayUnavailable, spawn

        mock_client.post.side_effect = httpx.ReadTimeout("gateway slow")

        with pytest.raises(GatewayUnavailable, match="no retry: non-idempotent"):
            spawn(spawner="user", prompt="hello", fork_from=None, prompt_source="user")
        assert mock_client.post.call_count == 1

    @patch("ava._gateway_transport._client")
    def test_spawn_connect_error_is_retried(self, mock_client: MagicMock):
        """Connect-family failures happen before the request reaches the
        server, so re-sending a spawn is safe — the retry stays."""
        from ava._gateway_client import spawn

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"id": 42}
        mock_client.post.side_effect = [httpx.ConnectError("refused"), mock_resp]

        agent_id = spawn(spawner="user", prompt="hello", fork_from=None, prompt_source="user")
        assert agent_id == 42
        assert mock_client.post.call_count == 2

    @patch("ava._gateway_transport._client")
    def test_spawn_read_timeout_after_connect_error_retries_connect_only(
        self, mock_client: MagicMock
    ):
        """Mixed failure: the first connect error is retried, but the read
        timeout that follows is terminal — the request may have landed."""
        from ava._gateway_client import GatewayUnavailable, spawn

        mock_client.post.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("gateway slow"),
        ]

        with pytest.raises(GatewayUnavailable, match="no retry: non-idempotent"):
            spawn(spawner="user", prompt="hello", fork_from=None, prompt_source="user")
        assert mock_client.post.call_count == 2


# --- send_message ---


class TestSendMessage:
    @patch("ava._gateway_transport._client")
    def test_send_message_fire_and_forget(self, mock_client: MagicMock):
        """send_message is pure POST + return, does not read the status field."""
        from ava._gateway_client import send_message

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 201
        mock_resp.is_success = True
        mock_client.post.return_value = mock_resp

        result = send_message(42, content="hello", source="user")
        assert result is None


# --- terminate / restart ---


class TestLifecycle:
    @pytest.mark.parametrize("wire_status", ["enqueued", "already_terminated"])
    @patch("ava._gateway_transport._client")
    def test_terminate_returns_status(self, mock_client: MagicMock, wire_status: str):
        from ava._gateway_client import terminate

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"status": wire_status}
        mock_client.post.return_value = mock_resp

        status = terminate(42, source="agent:1", force=True)
        assert status == wire_status
        assert mock_client.post.call_args.kwargs["json"]["force"] is True

    @patch("ava._gateway_transport._client")
    def test_restart_returns_status(self, mock_client: MagicMock):
        from ava._gateway_client import restart

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"status": "idling"}
        mock_client.post.return_value = mock_resp

        status = restart(42, source="agent:1")
        assert status == "idling"


# --- transient HTTP 429/5xx retry (task #960) ---


def _transient_resp(status: int, body: dict | None = None) -> MagicMock:
    """A response with the given HTTP status; wire JSON body when given, else
    a plain non-JSON body (FastAPI default error shape) whose
    raise_for_status raises like real httpx."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.is_success = 200 <= status < 300
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = json.JSONDecodeError("msg", "", 0)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=resp
        )
    return resp


class TestTransientHttpRetry:
    """Idempotent requests retry transient HTTP 429/5xx with bounded backoff.

    Regression for task #960: the 2026-08-07 memory-search transient 500
    crashed an agent's graph because the client had no HTTP-status retry and
    the before_llm caller saw an uncaught HTTPStatusError.
    """

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_memory_search_transient_500_self_heals(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """The incident scenario: a plain-text 500 (FastAPI default) followed
        by success. The retry rides out the blip; the caller sees results."""
        from ava._gateway_client import memory_search

        ok = _transient_resp(200, {"results": [{"path": "a.md", "description": "d", "tags": []}]})
        mock_client.post.side_effect = [_transient_resp(500), ok]

        results = memory_search("query", 5)
        assert [r.path for r in results] == ["a.md"]
        assert mock_client.post.call_count == 2

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_memory_search_dedicated_timeout_and_single_retry(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """Memory search carries its own budget (task #2003/A): the
        per-attempt timeout is the gateway's search deadline + margin (not
        the global AVA_GATEWAY_HTTP_TIMEOUT_SECONDS), and a persistent 503 is
        exhausted after ONE retry instead of stacking the default 3 — the
        gateway's own deadline has already spent that time, so re-sending only
        re-queues behind the same congestion."""
        from ava import _gateway_client as gc
        from shared.agents import IndexerUnavailable
        from shared.config import settings

        fail = _transient_resp(503, {"reason": "indexer_unavailable", "detail": "busy"})
        mock_client.post.return_value = fail

        with pytest.raises(IndexerUnavailable, match="busy"):
            gc.memory_search("query", 5)
        assert mock_client.post.call_count == 2

        # Per-attempt timeout derives from the gateway deadline + 3s margin.
        mock_client.post.side_effect = None
        mock_client.post.return_value = _transient_resp(
            200, {"results": [{"path": "a.md", "description": "", "tags": []}]}
        )
        gc.memory_search("query", 5)
        passed = mock_client.post.call_args.kwargs["timeout"]
        assert (
            passed.read
            == settings.services.memory_search_deadline_seconds + gc._MEMORY_SEARCH_TIMEOUT_MARGIN_S
        )

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_memory_search_caller_timeout_overrides_default(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """An explicit `timeout` replaces the derived default for that one call."""
        from ava._gateway_client import memory_search

        mock_client.post.return_value = _transient_resp(
            200, {"results": [{"path": "a.md", "description": "", "tags": []}]}
        )
        memory_search("query", 5, timeout=9.0)
        passed = mock_client.post.call_args.kwargs["timeout"]
        assert passed.read == 9.0

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_transient_5xx_exhausted_surfaces_wire_error(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """After retries are exhausted the wire contract is preserved: a 503
        with reason indexer_unavailable raises IndexerUnavailable (the error
        callers catch to degrade), not a generic transport error."""
        from ava._gateway_transport import _post, _raise_from_response
        from shared.agents import IndexerUnavailable

        fail = _transient_resp(503, {"reason": "indexer_unavailable", "detail": "embed failed"})
        mock_client.post.return_value = fail

        resp = _post("/api/memory/search", {"query": "q", "k": 5})
        assert mock_client.post.call_count == 3  # pyright: ignore[reportUnknownArgumentType]  # 3 attempts, all 503
        with pytest.raises(IndexerUnavailable, match="embed failed"):
            _raise_from_response(resp)  # pyright: ignore[reportUnknownArgumentType]

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_transient_5xx_not_retried_for_non_idempotent(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """spawn is non-idempotent: an HTTP 5xx means the route may have
        committed (agent row created) before erroring — no re-send."""
        from ava._gateway_client import spawn

        mock_client.post.return_value = _transient_resp(500)

        with pytest.raises(httpx.HTTPStatusError):
            spawn(spawner="user", prompt="hello", fork_from=None, prompt_source="user")
        assert mock_client.post.call_count == 1

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_get_transient_5xx_retried(self, mock_time: MagicMock, mock_client: MagicMock):
        from ava._gateway_transport import _get

        ok = _transient_resp(200)
        mock_client.get.side_effect = [_transient_resp(503), ok]

        resp = _get("/api/agents")
        assert resp is ok
        assert mock_client.get.call_count == 2

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_delete_transient_5xx_retried(self, mock_time: MagicMock, mock_client: MagicMock):
        from ava._gateway_transport import _delete

        ok = _transient_resp(204)
        mock_client.delete.side_effect = [_transient_resp(500), ok]

        resp = _delete("/api/agents/1/pages/x")
        assert resp is ok
        assert mock_client.delete.call_count == 2

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_429_retried(self, mock_time: MagicMock, mock_client: MagicMock):
        from ava._gateway_transport import _get

        ok = _transient_resp(200)
        mock_client.get.side_effect = [_transient_resp(429), ok]

        resp = _get("/api/agents")
        assert resp is ok
        assert mock_client.get.call_count == 2


class TestRetryBackoffJitter:
    """Bounded exponential backoff + deterministic per-agent jitter
    (heartbeat-daemon de-phasing pattern, task #960)."""

    def test_agent_jitter_zero_without_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_transport as gc

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        assert gc._agent_jitter_seconds() == 0.0

    def test_agent_jitter_deterministic_and_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_transport as gc

        monkeypatch.setenv("AVA_AGENT_ID", "1234")
        assert gc._agent_jitter_seconds() == gc._agent_jitter_seconds()  # deterministic
        assert 0.0 <= gc._agent_jitter_seconds() < gc._JITTER_SPAN_S

    def test_agent_jitter_differs_across_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_transport as gc

        monkeypatch.setenv("AVA_AGENT_ID", "1")
        a = gc._agent_jitter_seconds()
        monkeypatch.setenv("AVA_AGENT_ID", "2")
        b = gc._agent_jitter_seconds()
        assert a != b

    def test_backoff_bounded_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """1s → 2s → 4s → 8s cap (defaults), no jitter without an agent id."""
        from ava import _gateway_transport as gc

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        assert [gc._retry_delay_seconds(i) for i in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_sleeps_follow_backoff_schedule(
        self, mock_time: MagicMock, mock_client: MagicMock, monkeypatch: MagicMock
    ):
        """Retries sleep the bounded backoff schedule, not the old fixed 1s."""
        from ava._gateway_transport import _post

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        mock_client.post.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable):
            _post("/api/x")
        sleeps = [c.args[0] for c in mock_time.sleep.call_args_list]
        assert sleeps == [1.0, 2.0]  # attempts 0 and 1; no jitter without agent id


class TestSendMessageAtLeastOnceWithKey:
    """send_message is a pure INSERT behind an AtLeastOnceWithKey doorplate
    (R3 door ①): every retry of one logical message carries the same
    `Idempotency-Key` header and the server dedups, so the full transient
    family is retried safely — no duplicate inbound even when a retry lands
    after the first attempt already committed."""

    @patch("ava._gateway_transport._client")
    def test_send_message_retries_read_timeout_with_one_key(self, mock_client: MagicMock):
        from ava._gateway_client import GatewayUnavailable, send_message

        mock_client.post.side_effect = httpx.ReadTimeout("gateway slow")

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            send_message(42, content="hello", source="user")
        assert mock_client.post.call_count == 3
        keys = {
            mock_client.post.call_args_list[i].kwargs["headers"]["Idempotency-Key"]
            for i in range(3)
        }
        assert len(keys) == 1, "all retries of one message must share one key"
        assert next(iter(keys))

    @patch("ava._gateway_transport._client")
    def test_send_message_retries_transient_5xx(self, mock_client: MagicMock):
        from ava._gateway_client import send_message

        resp = _transient_resp(500)
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err
        mock_client.post.return_value = resp

        with pytest.raises(httpx.HTTPStatusError):
            send_message(42, content="hello", source="user")
        assert mock_client.post.call_count == 3  # outcome unknown but deduped by key

    @patch("ava._gateway_transport._client")
    @patch("ava._gateway_transport._time")
    def test_send_message_503_without_reason_surfaces_status_code(
        self, mock_time: MagicMock, mock_client: MagicMock
    ):
        """Regression for task #1205 (2026-08-12 cluster-update report): a
        gateway 503 with a FastAPI-default body (no wire `reason`) must raise
        HTTPStatusError carrying 503 — not `KeyError: 'reason'` first, with
        the status code masked."""
        from ava._gateway_client import send_message

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        mock_client.post.return_value = httpx.Response(
            503, json={"detail": "gateway unavailable"}, request=request
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            send_message(42, content="hello", source="user")
        assert excinfo.value.response.status_code == 503
        assert mock_client.post.call_count == 3  # AtLeastOnceWithKey retries, deduped by key
        assert not isinstance(excinfo.value.__context__, KeyError)

    @patch("ava._gateway_transport._client")
    def test_send_message_sends_key_header(self, mock_client: MagicMock):
        from ava._gateway_client import send_message

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 201
        ok.is_success = True
        mock_client.post.return_value = ok

        send_message(42, content="hello", source="user")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers and headers.get("Idempotency-Key")
