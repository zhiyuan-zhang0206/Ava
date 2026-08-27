"""SDK ↔ Gateway error wire-equivalence.

Locks in the "server raise X → handler encodes typed envelope + reason → SDK parse → reconstruct X"
end-to-end invariant.

Every AvaAgentError subclass is parametrized to run two assertions:
  1. **handler encoding correct**: register handler on an isolated FastAPI app + a synthetic
     endpoint that raises cls; TestClient hits it; checks the typed envelope plus
     body.reason==cls.reason / body.detail matches the raised message
  2. **SDK reconstruction correct**: feed a synthetic httpx.Response to _gateway_client.
     _raise_from_response; checks the raised exception is the same cls + same message

When adding a new AvaAgentError subclass, must also add it to EXCEPTION_BY_REASON — this test
auto-parametrizes and won't miss coverage.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava._gateway_client import _raise_from_response
from gateway.app import _ava_agent_error_handler
from shared.agents import EXCEPTION_BY_REASON, AvaAgentError


@pytest.mark.parametrize(
    ("reason", "cls"),
    list(EXCEPTION_BY_REASON.items()),
    ids=[r.value for r in EXCEPTION_BY_REASON],
)
def test_handler_emits_expected_wire(reason, cls):
    """Server-side: handler encodes the full envelope plus SDK wire reason."""
    app = FastAPI()
    # FastAPI's add_exception_handler second parameter is typed as ExceptionHandler
    # (Exception input); our handler input is an AvaAgentError subtype — pyright
    # considers it invariant-narrowed and disallows it. Runtime dispatch uses the registered
    # exception type, so it's actually safe.
    app.add_exception_handler(AvaAgentError, _ava_agent_error_handler)  # type: ignore[arg-type]

    @app.get("/_raise")
    def _raise():
        raise cls("forced wire-test message")

    with TestClient(app) as client:
        resp = client.get("/_raise")

    assert resp.status_code == cls.http_status
    body = resp.json()
    assert body["type"] == "about:blank"
    assert body["code"] == reason.value
    assert body["status"] == cls.http_status
    assert body["reason"] == reason
    assert body["detail"] == "forced wire-test message"
    assert body["retryable"] is False
    assert isinstance(body["trace_id"], str) and body["trace_id"]


@pytest.mark.parametrize(
    ("reason", "cls"),
    list(EXCEPTION_BY_REASON.items()),
    ids=[r.value for r in EXCEPTION_BY_REASON],
)
def test_sdk_reconstructs_from_wire(reason, cls):
    """client-side: feed synthetic wire body to _raise_from_response; it reconstructs same
    cls + message."""
    body = {"detail": "forced wire-test message", "reason": reason.value}
    resp = httpx.Response(
        status_code=cls.http_status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with pytest.raises(cls, match="forced wire-test message"):
        _raise_from_response(resp)
