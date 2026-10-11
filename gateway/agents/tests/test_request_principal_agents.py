"""Chat delivery and reconciliation share one stored message identity."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from base.api_contracts.idempotency import chat_message_key
from gateway.http.auth.request_principal import AuthPrincipal


def _request(scope: str | None = "principal-v1", principal: AuthPrincipal | None = None) -> Request:
    headers = [] if scope is None else [(b"idempotency-scope", scope.encode())]
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/example", "headers": headers}
    )
    request.state.auth_principal = principal
    return request


def test_reconciliation_uses_the_original_message_identity() -> None:
    from gateway.agents.state import _scoped_message_key

    request = _request(principal=AuthPrincipal("cluster", "administrator"))
    assert _scoped_message_key(request, "k") == chat_message_key("k")


@pytest.mark.parametrize("scope", [None, "principal-v2"])
def test_message_key_requires_the_principal_scope(scope: str | None) -> None:
    from gateway.agents.state import _scoped_message_key

    request = _request(scope, AuthPrincipal("cluster", "administrator"))
    with pytest.raises(HTTPException) as caught:
        _scoped_message_key(request, "k")
    assert caught.value.status_code == 422


def test_message_key_requires_a_verified_principal() -> None:
    from gateway.agents.state import _scoped_message_key

    with pytest.raises(HTTPException) as caught:
        _scoped_message_key(_request(), "k")
    assert caught.value.status_code == 422
