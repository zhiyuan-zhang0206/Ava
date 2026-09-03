"""Credential scopes cannot be chosen by caller labels or raw storage keys."""

import pytest
from starlette.requests import Request

from gateway.request_principal import AuthPrincipal, PrincipalScopeError, principal_key, request_key


def _request(scope: str | None = "principal-v1", principal: AuthPrincipal | None = None) -> Request:
    headers = [] if scope is None else [(b"idempotency-scope", scope.encode())]
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/example", "headers": headers}
    )
    request.state.auth_principal = principal
    return request


def test_same_principal_operation_and_key_are_stable() -> None:
    principal = AuthPrincipal("mcp_client", "42")
    assert principal_key(principal, "post", "/api/example", "retry-1") == principal_key(
        principal, "POST", "/api/example", "retry-1"
    )


def test_distinct_credentials_and_operations_cannot_collide() -> None:
    principals = [
        AuthPrincipal("cluster", "administrator"),
        AuthPrincipal("mcp_client", "2"),
        AuthPrincipal("mcp_client", "3"),
        AuthPrincipal("mcp_client", "1"),
    ]
    keys = {
        principal_key(p, method, path, "same-key")
        for p in principals
        for method in ["POST", "DELETE"]
        for path in ["/api/a", "/api/b"]
    }
    assert len(keys) == 16


def test_labels_do_not_select_principal() -> None:
    request = _request(principal=AuthPrincipal("mcp_client", "42"))
    request.state.caller_identity = {"kind": "human", "subject": "admin"}
    assert request_key(request, "k", method="POST", path="/api/a") == principal_key(
        AuthPrincipal("mcp_client", "42"), "POST", "/api/a", "k"
    )


def test_unverified_principal_is_rejected_without_downgrade() -> None:
    request = _request()
    request.state.auth_principal = {"kind": "cluster", "subject": "admin"}
    with pytest.raises(PrincipalScopeError, match="verified credential"):
        request_key(request, "k", method="POST", path="/api/a")


def test_legacy_keys_remain_unchanged_except_reserved_storage_namespace() -> None:
    request = _request(scope=None)
    assert request_key(request, "existing-retry", method="POST", path="/api/a") == "existing-retry"
    protected = principal_key(AuthPrincipal("mcp_client", "1"), "POST", "/api/a", "k")
    with pytest.raises(PrincipalScopeError, match="reserved"):
        request_key(request, protected, method="POST", path="/api/a")


@pytest.mark.parametrize("scope", ["", "source", "principal-v2", "human"])
def test_unknown_scope_fails_loudly(scope: str) -> None:
    with pytest.raises(PrincipalScopeError, match="unsupported"):
        request_key(_request(scope), "k", method="POST", path="/api/a")


def test_reconciliation_uses_original_message_operation_namespace() -> None:
    from gateway.routers.agents_state import _scoped_message_key

    request = _request(principal=AuthPrincipal("mcp_client", "1"))
    assert _scoped_message_key(request, 42, "k") == principal_key(
        AuthPrincipal("mcp_client", "1"), "POST", "/api/agents/42/messages", "k"
    )


def test_mcp_principal_cannot_opt_out_by_omitting_header() -> None:
    principal = AuthPrincipal("mcp_client", "42")
    request = _request(scope=None, principal=principal)
    assert request_key(request, "k", method="POST", path="/api/a") == principal_key(
        principal, "POST", "/api/a", "k"
    )
