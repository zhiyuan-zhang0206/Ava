from __future__ import annotations

import pytest

from base.api_contracts import contracts
from base.api_contracts.contracts import Idempotency, PauseSemantics


def test_sdk_inherits_idempotency_from_contracts() -> None:
    assert (
        contracts.idempotency_for("POST", "/api/keyed/v1/agents")
        is Idempotency.AT_LEAST_ONCE_WITH_KEY
    )
    assert (
        contracts.idempotency_for("POST", "/api/agents/7/messages")
        is Idempotency.AT_LEAST_ONCE_WITH_KEY
    )
    assert contracts.idempotency_for("GET", "/api/agents/7") is Idempotency.IDEMPOTENT
    guarded_contract = contracts.contract_for("POST", "/api/keyed/v1/agents")
    assert guarded_contract is not None
    assert guarded_contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY
    reconcile_contract = contracts.contract_for("POST", "/api/agents/7/messages/reconcile")
    assert reconcile_contract is not None
    assert reconcile_contract.idempotency is Idempotency.IDEMPOTENT


def test_unknown_route_defaults_to_non_idempotent() -> None:
    assert contracts.idempotency_for("POST", "/api/agenst") is Idempotency.NON_IDEMPOTENT
    assert contracts.idempotency_for("POST", "/api/no-such-route") is Idempotency.NON_IDEMPOTENT
    assert contracts.idempotency_for("DELETE", "/api/agents/7") is Idempotency.NON_IDEMPOTENT


@pytest.mark.parametrize(
    ("template", "path", "expected"),
    [
        ("/api/agents/{agent_id}/messages", "/api/agents/123/messages", True),
        ("/api/agents/{agent_id}/messages", "/api/agents/123/messages/x", False),
        ("/pages/{page_key}/{rest:path}", "/pages/5-report/a/b/c", True),
        ("/pages/{page_key}/{rest:path}", "/pages/5-report", False),
        ("/api/agents/{agent_id}/terminate", "/api/agents/42/terminate", True),
        ("/api/agents/{agent_id}/terminate", "/api/agents/42/restart", False),
        ("/api/cluster/machines/{name}", "/api/cluster/machines/node-1", True),
        ("/api/cluster/machines/{name}", "/api/cluster/machines/node-1/x", False),
    ],
)
def test_template_matching(template: str, path: str, expected: bool) -> None:
    assert contracts.match_path(template, path) is expected


def test_contracts_have_no_unknown_semantics() -> None:
    for (method, _path), c in contracts.ROUTE_CONTRACTS.items():
        assert c.idempotency in Idempotency, f"bad idempotency on {method} {_path}"
        assert c.pause in PauseSemantics, f"bad pause on {method} {_path}"


@pytest.mark.parametrize(
    "method,path",
    [
        ("PATCH", "/api/agents/7/notices/current/guarded-v1"),
        ("POST", "/api/agents/7/notices/current/dismiss/guarded-v1"),
        ("POST", "/api/keyed/v1/agents/7/notices/11/resolve"),
    ],
)
def test_guarded_notice_routes_are_keyed(method: str, path: str) -> None:
    contract = contracts.contract_for(method, path)
    assert contract is not None
    assert contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY
