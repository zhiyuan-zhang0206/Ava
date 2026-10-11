"""Credential scopes cannot be chosen by caller labels or raw storage keys."""

import pytest
from starlette.requests import Request

from gateway.http.auth.request_principal import (
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
    request_key,
)


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


def test_missing_scope_is_rejected_for_every_principal() -> None:
    for principal in (AuthPrincipal("cluster", "administrator"), AuthPrincipal("mcp_client", "42")):
        with pytest.raises(PrincipalScopeError, match="requires the header"):
            request_key(_request(None, principal), "k", method="POST", path="/api/a")


@pytest.mark.parametrize("scope", ["", "source", "principal-v2", "human"])
def test_unknown_scope_fails_loudly(scope: str) -> None:
    principal = AuthPrincipal("cluster", "administrator")
    with pytest.raises(PrincipalScopeError, match="unsupported"):
        request_key(_request(scope, principal), "k", method="POST", path="/api/a")


def test_stored_prefix_is_not_a_raw_namespace() -> None:
    principal = AuthPrincipal("cluster", "administrator")
    protected = principal_key(principal, "POST", "/api/a", "k")
    assert request_key(
        _request(principal=principal), protected, method="POST", path="/api/a"
    ) == principal_key(principal, "POST", "/api/a", protected)


@pytest.mark.parametrize("scope", [None, "principal-v1"])
@pytest.mark.parametrize("key", ["", "x" * 129])
def test_invalid_keys_fail_before_scope_is_consulted(scope: str | None, key: str) -> None:
    request = _request(scope, AuthPrincipal("cluster", "administrator"))
    with pytest.raises(PrincipalScopeError, match="1 to 128"):
        request_key(request, key, method="POST", path="/api/a")


def test_maximum_length_key_is_accepted() -> None:
    principal = AuthPrincipal("cluster", "administrator")
    key = "x" * 128
    assert request_key(
        _request(principal=principal), key, method="POST", path="/api/a"
    ) == principal_key(principal, "POST", "/api/a", key)
