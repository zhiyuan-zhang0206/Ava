"""Reconciliation uses the original message's operation namespace."""

from starlette.requests import Request

from gateway.auth.request_principal import AuthPrincipal, principal_key


def _request(scope: str | None = "principal-v1", principal: AuthPrincipal | None = None) -> Request:
    headers = [] if scope is None else [(b"idempotency-scope", scope.encode())]
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/example", "headers": headers}
    )
    request.state.auth_principal = principal
    return request


def test_reconciliation_uses_original_message_operation_namespace() -> None:
    from gateway.agents.state import _scoped_message_key

    request = _request(principal=AuthPrincipal("mcp_client", "1"))
    assert _scoped_message_key(request, 42, "k") == principal_key(
        AuthPrincipal("mcp_client", "1"), "POST", "/api/agents/42/messages", "k"
    )
