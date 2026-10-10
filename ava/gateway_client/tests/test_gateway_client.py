"""ava/gateway_client/__init__.py tests.

Covers:
- raise_from_response: wire contract error reconstruction
- post / get: retry logic + network-layer errors → GatewayUnavailable
- spawn / send_message: public API happy + error paths
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

import ava
from ava.gateway_client.transport import use_client
from ava.sdk_surface.install import Installation
from base.agents import GatewayUnavailable
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture()
def mock_client(
    monkeypatch: pytest.MonkeyPatch, model_installation: Installation
) -> Iterator[MagicMock]:
    """Bind the wire client through its public seam and supply its explicit SDK owner."""
    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)
    client = MagicMock()
    with use_client(client):
        yield client


# --- raise_from_response ---


class TestRaiseFromResponse:
    def test_success_does_not_raise(self):
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = True
        raise_from_response(resp)  # no raise

    def test_wire_json_error_reconstructed(self):
        """HTTP 400 + body {"reason":"agent_not_found","detail":"..."} → AgentNotFound."""
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 404
        resp.json.return_value = {"reason": "agent_not_found", "detail": "agent 99 not found"}

        from base.agents import AgentNotFound

        with pytest.raises(AgentNotFound, match="agent 99 not found"):
            raise_from_response(resp)

    def test_non_json_body_falls_through_to_http_error(self):
        """body is not JSON → raise_for_status raises HTTPStatusError."""
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 500
        resp.json.side_effect = json.JSONDecodeError("msg", "", 0)
        # Mock raise_for_status to actually raise
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(resp)

    def test_corrupted_content_encoding_falls_through(self):
        """A response body whose Content-Encoding fails to decode raises
        httpx.DecodingError (0.28.1, broken gzip/br stream) — same
        protocol-mismatch class as non-JSON: falls through to the clean
        HTTPStatusError instead of the DecodingError masking the status.

        Regression: _wire_reason only caught JSONDecodeError (task #1669).
        """
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 502
        resp.json.side_effect = httpx.DecodingError("corrupted gzip stream")
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        # The status was surfaced as the primary error — the DecodingError
        # never escapes as the exception seen by the caller.
        assert excinfo.value.response.status_code == 502
        assert not isinstance(excinfo.value.__context__, httpx.DecodingError)

    def test_missing_reason_field_falls_through(self):
        """JSON present but missing reason field → raise_for_status."""
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 400
        resp.json.return_value = {"detail": "something"}
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(resp)

    def test_invalid_reason_value_falls_through(self):
        """reason value not in ErrorReason enum → ValueError → raise_for_status."""
        from ava.gateway_client.transport import raise_from_response

        resp = MagicMock(spec=httpx.Response)
        resp.is_success = False
        resp.status_code = 400
        resp.json.return_value = {"reason": "garbage_value", "detail": "x"}
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(resp)

    def test_valid_reason_missing_detail_falls_through(self):
        """Valid `reason` but `detail` field missing → HTTPStatusError, not
        KeyError: 'detail' masking the status code (same class as task #1205).

        Regression: the reverse-lookup raise indexed `body["detail"]` without
        a guard, so a reason-bearing body with no detail leaked a raw KeyError
        and the HTTP status code never reached the caller.
        """
        from ava.gateway_client.transport import raise_from_response

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        resp = httpx.Response(404, json={"reason": "agent_not_found"}, request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        assert excinfo.value.response.status_code == 404
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_valid_reason_non_string_detail_falls_through(self):
        """Valid `reason` but non-string `detail` → HTTPStatusError with the
        status code; a malformed detail is a protocol mismatch, not an
        application error to reconstruct."""
        from ava.gateway_client.transport import raise_from_response

        request = httpx.Request("GET", "http://gw/api/agents/7")
        resp = httpx.Response(
            404, json={"reason": "agent_not_found", "detail": {"nested": True}}, request=request
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
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
        from ava.gateway_client.transport import raise_from_response

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        resp = httpx.Response(503, json={"detail": "gateway unavailable"}, request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        assert excinfo.value.response.status_code == 503
        # The HTTP error is the primary exception: no parse KeyError chained
        # as __context__ masking the original status code.
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_non_object_json_body_falls_through(self):
        """JSON body that is not an object (no `reason` possible) → raise_for_status."""
        from ava.gateway_client.transport import raise_from_response

        request = httpx.Request("GET", "http://gw/api/agents")
        resp = httpx.Response(503, json=["not", "an", "object"], request=request)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        assert excinfo.value.response.status_code == 503
        assert not isinstance(excinfo.value.__context__, KeyError)


# --- post retry ---


class TestPostRetry:
    def test_first_attempt_succeeds(self, mock_client: MagicMock):
        from ava.gateway_client.transport import post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp

        result = post("/api/agents", {"key": "val"})
        assert result is mock_resp
        assert mock_client.post.call_count == 1

    def test_retries_on_transport_error(self, mock_client: MagicMock, retry_waits: list[float]):
        from ava.gateway_client.transport import post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        # First two fail, third succeeds. POST /api/agents is a
        # NON_IDEMPOTENT spawn (read timeout must NOT retry — twin risk), so
        # this retry test uses a read-only POST endpoint instead.
        mock_client.post.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timeout"),
            mock_resp,
        ]

        result = post("/api/memory/search")
        assert result is mock_resp
        assert mock_client.post.call_count == 3
        assert len(retry_waits) == 2

    @pytest.mark.usefixtures("retry_waits")
    def test_all_retries_exhausted_raises_gateway_unavailable(self, mock_client: MagicMock):
        from ava.gateway_client.transport import post

        mock_client.post.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            post("/api/agents")
        assert mock_client.post.call_count == 3

    def test_http_4xx_not_retried(self, mock_client: MagicMock):
        """HTTP 4xx is an application error, no retry."""
        from ava.gateway_client.transport import post

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.is_success = False
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"reason": "agent_not_found", "detail": "x"}
        mock_client.post.return_value = mock_resp

        # post returns the response, raise_from_response is called by the caller
        result = post("/api/agents")
        assert result is mock_resp
        assert mock_client.post.call_count == 1  # no retry


# --- post timeout contract ---


class TestPostTimeoutContract:
    """What `post` hands httpx, not what it was asked for.

    httpx distinguishes three things a caller can mean by `timeout`: a value
    (use it), the `USE_CLIENT_DEFAULT` sentinel (fall back to the client's
    configured timeout), and `None` (**never** time out). Only the sentinel
    falls back, so these assert the argument httpx actually receives — asserting
    `post`'s own default would have passed all along while every POST in the
    SDK ran unbounded and `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS` did nothing.
    """

    def test_default_defers_to_client_timeout(self, mock_client: MagicMock):
        """No per-call timeout → httpx gets the sentinel, never None."""
        from ava.gateway_client.transport import post

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        mock_client.post.return_value = ok

        post("/api/memory/search", {"query": "x", "k": 5})

        passed = mock_client.post.call_args.kwargs["timeout"]
        assert passed is httpx.USE_CLIENT_DEFAULT
        assert passed is not None

    def test_explicit_timeout_is_forwarded(self, mock_client: MagicMock):
        """A per-call timeout still overrides the client default."""
        from ava.gateway_client.transport import post

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        mock_client.post.return_value = ok
        per_call = httpx.Timeout(120.0)

        post("/api/agents/1/messages", {"content": "hi"}, timeout=per_call)

        assert mock_client.post.call_args.kwargs["timeout"] is per_call


# --- get retry ---


class TestGetRetry:
    def test_first_attempt_succeeds(self, mock_client: MagicMock):
        from ava.gateway_client.transport import get

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        result = get("/api/agents/1")
        assert result is mock_resp
        assert mock_client.get.call_count == 1

    @pytest.mark.usefixtures("retry_waits")
    def test_all_retries_exhausted_raises_gateway_unavailable(self, mock_client: MagicMock):
        from ava.gateway_client.transport import get

        mock_client.get.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable):
            get("/api/agents/1")
        assert mock_client.get.call_count == 3


# --- spawn ---


class TestSendMessage:
    def test_send_message_fire_and_forget(self, mock_client: MagicMock):
        """send_message is pure POST + return, does not read the status field."""
        from ava.gateway_client import send_message

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 201
        mock_resp.is_success = True
        mock_client.post.return_value = mock_resp

        result = send_message(42, content="hello", source="user")
        assert result is None


# --- terminate / restart ---


class TestLifecycle:
    @pytest.mark.parametrize("wire_status", ["enqueued", "already_terminated"])
    def test_terminate_returns_status(self, mock_client: MagicMock, wire_status: str):
        from ava.gateway_client import terminate

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"status": wire_status, "open_tasks": None}
        mock_client.post.return_value = mock_resp

        result = terminate(42, source="agent:1", force=True)
        # The client hands back the full wire payload — status plus the
        # open-tasks hint — no longer just the status string (task #3361).
        assert result == {"status": wire_status, "open_tasks": None}
        assert mock_client.post.call_args.kwargs["json"]["force"] is True

    def test_terminate_passes_open_tasks_hint_through(self, mock_client: MagicMock):
        """A non-null hint rides the client verbatim; the SDK converts it."""
        from ava.gateway_client import terminate

        hint = {
            "count": 6,
            "tasks": [
                {
                    "id": 11,
                    "title": "sync terminate tests",
                    "status": "in_progress",
                    "updated_at": "2026-09-14T05:00:00+00:00",
                }
            ],
            "more": 1,
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"status": "enqueued", "open_tasks": hint}
        mock_client.post.return_value = mock_resp

        result = terminate(42)
        assert result == {"status": "enqueued", "open_tasks": hint}

    def test_restart_returns_status(self, mock_client: MagicMock):
        from ava.gateway_client import restart

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
    """Idempotent requests retry known transient statuses with bounded backoff."""

    @pytest.mark.usefixtures("retry_waits")
    def test_memory_search_unknown_500_is_exposed_once(self, mock_client: MagicMock):
        """An unhandled server failure must not be retried as backend congestion."""
        from ava.gateway_client import memory_search

        ok = _transient_resp(200, {"results": [{"path": "a.md", "description": "d", "tags": []}]})
        mock_client.post.side_effect = [_transient_resp(500), ok]

        with pytest.raises(httpx.HTTPStatusError):
            memory_search("query", 5)
        assert mock_client.post.call_count == 1

    @pytest.mark.usefixtures("retry_waits")
    def test_memory_search_dedicated_timeout_and_single_retry(self, mock_client: MagicMock):
        """Memory search carries its own budget (task #2003/A): the
        per-attempt timeout is the gateway's search deadline + margin (not
        the global AVA_GATEWAY_HTTP_TIMEOUT_SECONDS), and a persistent 503 is
        exhausted after ONE retry instead of stacking the default 3 — the
        gateway's own deadline has already spent that time, so re-sending only
        re-queues behind the same congestion."""
        from ava import gateway_client as gc
        from base.agents import IndexerUnavailable
        from base.config import settings

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

    @pytest.mark.usefixtures("retry_waits")
    def test_memory_search_caller_timeout_overrides_default(self, mock_client: MagicMock):
        """An explicit `timeout` replaces the derived default for that one call."""
        from ava.gateway_client import memory_search

        mock_client.post.return_value = _transient_resp(
            200, {"results": [{"path": "a.md", "description": "", "tags": []}]}
        )
        memory_search("query", 5, timeout=9.0)
        passed = mock_client.post.call_args.kwargs["timeout"]
        assert passed.read == 9.0

    @pytest.mark.usefixtures("retry_waits")
    def test_transient_5xx_exhausted_surfaces_wire_error(self, mock_client: MagicMock):
        """After retries are exhausted the wire contract is preserved: a 503
        with reason indexer_unavailable raises IndexerUnavailable (the error
        callers catch to degrade), not a generic transport error."""
        from ava.gateway_client.transport import post, raise_from_response
        from base.agents import IndexerUnavailable

        fail = _transient_resp(503, {"reason": "indexer_unavailable", "detail": "embed failed"})
        mock_client.post.return_value = fail

        resp = post("/api/memory/search", {"query": "q", "k": 5})
        assert mock_client.post.call_count == 3  # pyright: ignore[reportUnknownArgumentType]  # 3 attempts, all 503
        with pytest.raises(IndexerUnavailable, match="embed failed"):
            raise_from_response(resp)  # pyright: ignore[reportUnknownArgumentType]

    @pytest.mark.usefixtures("retry_waits")
    def test_transient_5xx_not_retried_for_non_idempotent(self, mock_client: MagicMock):
        """spawn is non-idempotent: an HTTP 5xx means the route may have
        committed (agent row created) before erroring — no re-send."""
        from ava.gateway_client import spawn

        mock_client.post.return_value = _transient_resp(500)

        with pytest.raises(httpx.HTTPStatusError):
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key=str(uuid4()),
            )
        assert mock_client.post.call_count == 1

    @pytest.mark.usefixtures("retry_waits")
    def test_get_transient_5xx_retried(self, mock_client: MagicMock):
        from ava.gateway_client.transport import get

        ok = _transient_resp(200)
        mock_client.get.side_effect = [_transient_resp(503), ok]

        resp = get("/api/agents")
        assert resp is ok
        assert mock_client.get.call_count == 2

    @pytest.mark.usefixtures("retry_waits")
    def test_delete_transient_5xx_retried(self, mock_client: MagicMock):
        from ava.gateway_client.transport import _delete

        ok = _transient_resp(204)
        mock_client.delete.side_effect = [_transient_resp(503), ok]

        resp = _delete("/api/presets/1")
        assert resp is ok
        assert mock_client.delete.call_count == 2

    @pytest.mark.usefixtures("retry_waits")
    def test_429_retried(self, mock_client: MagicMock):
        from ava.gateway_client.transport import get

        ok = _transient_resp(200)
        mock_client.get.side_effect = [_transient_resp(429), ok]

        resp = get("/api/agents")
        assert resp is ok
        assert mock_client.get.call_count == 2


class TestRetryBackoffJitter:
    """Bounded exponential backoff + deterministic per-agent jitter
    (heartbeat-daemon de-phasing pattern, task #960)."""

    def test_agent_jitter_zero_without_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava.gateway_client import transport as gc

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        pin_no_identity()
        assert gc._agent_jitter_seconds() == 0.0

    def test_agent_jitter_deterministic_and_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava.gateway_client import transport as gc

        pin_agent(1234)
        assert gc._agent_jitter_seconds() == gc._agent_jitter_seconds()  # deterministic
        assert 0.0 <= gc._agent_jitter_seconds() < gc._JITTER_SPAN_S

    def test_agent_jitter_differs_across_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava.gateway_client import transport as gc

        pin_agent(1)
        a = gc._agent_jitter_seconds()
        pin_agent(2)
        b = gc._agent_jitter_seconds()
        assert a != b

    def test_backoff_bounded_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """1s → 2s → 4s → 8s cap (defaults), no jitter without an agent id."""
        from ava.gateway_client import transport as gc

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        pin_no_identity()
        assert [gc._retry_delay_seconds(i) for i in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    def test_sleeps_follow_backoff_schedule(
        self, mock_client: MagicMock, retry_waits: list[float], monkeypatch: pytest.MonkeyPatch
    ):
        """Retries sleep the bounded backoff schedule, not the old fixed 1s."""
        from ava.gateway_client.transport import post

        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        ava.bind_context(replace(ava.context, identity=None))
        mock_client.post.side_effect = httpx.ConnectError("refused")

        with pytest.raises(GatewayUnavailable):
            post("/api/x")
        assert retry_waits == [1.0, 2.0]  # attempts 0 and 1; no jitter without agent id


class TestSendMessageAtLeastOnceWithKey:
    """send_message is a pure INSERT behind an AtLeastOnceWithKey doorplate
    (R3 door ①): every retry of one logical message carries the same
    `Idempotency-Key` header and the server dedups, so the full transient
    family is retried safely — no duplicate inbound even when a retry lands
    after the first attempt already committed."""

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("gateway slow"),
            httpx.WriteTimeout("write stalled"),
            httpx.ReadError("reply lost"),
        ],
    )
    @pytest.mark.usefixtures("retry_waits")
    def test_send_message_retries_transport_failure_with_one_key(
        self, mock_client: MagicMock, error: httpx.TransportError
    ):
        from ava.gateway_client import GatewayUnavailable, send_message

        mock_client.post.side_effect = error

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            send_message(42, content="hello", source="user")
        assert mock_client.post.call_count == 3
        keys = {
            mock_client.post.call_args_list[i].kwargs["headers"]["Idempotency-Key"]
            for i in range(3)
        }
        assert len(keys) == 1, "all retries of one message must share one key"
        assert next(iter(keys))

    @pytest.mark.usefixtures("retry_waits")
    def test_send_message_unknown_500_is_exposed_once(self, mock_client: MagicMock):
        from ava.gateway_client import send_message

        resp = _transient_resp(500)
        http_err = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = http_err
        mock_client.post.return_value = resp

        with pytest.raises(httpx.HTTPStatusError):
            send_message(42, content="hello", source="user")
        assert mock_client.post.call_count == 1

    @pytest.mark.usefixtures("retry_waits")
    def test_send_message_503_without_reason_surfaces_status_code(self, mock_client: MagicMock):
        """Regression for task #1205 (2026-08-12 cluster-update report): a
        gateway 503 with a FastAPI-default body (no wire `reason`) must raise
        HTTPStatusError carrying 503 — not `KeyError: 'reason'` first, with
        the status code masked."""
        from ava.gateway_client import send_message

        request = httpx.Request("POST", "http://gw/api/agents/42/messages")
        mock_client.post.return_value = httpx.Response(
            503, json={"detail": "gateway unavailable"}, request=request
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            send_message(42, content="hello", source="user")
        assert excinfo.value.response.status_code == 503
        assert mock_client.post.call_count == 3  # AtLeastOnceWithKey retries, deduped by key
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_send_message_sends_key_header(self, mock_client: MagicMock):
        from ava.gateway_client import send_message

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 201
        ok.is_success = True
        mock_client.post.return_value = ok

        send_message(42, content="hello", source="user")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers and headers.get("Idempotency-Key")


def test_retry_settings_are_read_when_a_request_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attempt count and base delay come from the settings at use, not from values
    captured when the module imported."""
    from ava.gateway_client import transport
    from base.config import settings

    monkeypatch.setattr(settings.gateway, "gateway_client_max_retries", 7)
    monkeypatch.setattr(settings.gateway, "gateway_client_retry_delay_seconds", 0.5)
    assert transport._max_retries() == 7
    assert transport._base_retry_delay_s() == 0.5
    assert transport._retry_delay_seconds(0) >= 0.5
