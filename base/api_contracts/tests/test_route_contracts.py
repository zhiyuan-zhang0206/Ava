"""Doorplate lint (R3 doors ① + ②): the contract wall is machine-supervised.

Every gateway route must declare a contract (`base/api_contracts/contracts.py` — the
single fact source), every declaration must be used, the pause-exempt
surface must be exactly the audited CONTROL_PLANE set (a new exemption is
a deliberate, reviewed change, not an incident patch), and the middleware
decision function must agree with the surface.

Invariant 1 (declared at the boundary definition) and invariant 2 (server
promises, clients inherit) are only as strong as this test — it is the
"tests supervise" row of the doorplate table.
"""

from __future__ import annotations

import pytest

from base.api_contracts import contracts
from base.api_contracts.contracts import Idempotency, PauseSemantics

# The audited exempt surface: exactly the surfaces that must stay reachable
# mid-migration. Adding a route here is a deliberate control-plane decision
# (it survives a migration and skips the pause 503); anything else is
# data-plane by default.


def test_sdk_inherits_idempotency_from_contracts() -> None:
    """The three semantics resolve where the SDK looks them up."""
    assert contracts.idempotency_for("POST", "/api/agents") is Idempotency.AT_LEAST_ONCE_WITH_KEY
    assert (
        contracts.idempotency_for("POST", "/api/agents/7/messages")
        is Idempotency.AT_LEAST_ONCE_WITH_KEY
    )
    assert contracts.idempotency_for("GET", "/api/agents/7") is Idempotency.IDEMPOTENT
    creation_contract = contracts.contract_for("POST", "/api/agents")
    assert creation_contract is not None and creation_contract.transactional_idempotency
    assert not creation_contract.legacy_keyed_retry
    guarded_contract = contracts.contract_for("POST", "/api/keyed/v1/agents")
    assert guarded_contract is not None and guarded_contract.transactional_idempotency
    assert guarded_contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY
    assert not guarded_contract.legacy_keyed_retry
    message_contract = contracts.contract_for("POST", "/api/agents/7/messages")
    assert message_contract is not None and message_contract.transactional_idempotency
    reconcile_contract = contracts.contract_for("POST", "/api/agents/7/messages/reconcile")
    assert reconcile_contract is not None and not reconcile_contract.transactional_idempotency


def test_unknown_route_defaults_to_non_idempotent() -> None:
    """An undeclared / misspelled path must never be blindly retried.

    R3 door ① ruling (2026-08-10): unknown routes default to
    NON_IDEMPOTENT — a retry could duplicate the side effect of a POST the
    doorplate never promised to dedup (#698 spawn-duplicate class). The
    previous IDEMPOTENT default made a typo'd path silently retryable.
    """
    assert (
        contracts.idempotency_for("POST", "/api/agenst")  # typo
        is Idempotency.NON_IDEMPOTENT
    )
    assert contracts.idempotency_for("POST", "/api/no-such-route") is Idempotency.NON_IDEMPOTENT
    assert (
        contracts.idempotency_for("DELETE", "/api/agents/7")  # undeclared method
        is Idempotency.NON_IDEMPOTENT
    )


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
    """Sanity: every declared contract uses known enum values."""
    for (method, _path), c in contracts.ROUTE_CONTRACTS.items():
        assert c.idempotency in Idempotency, f"bad idempotency on {method} {_path}"
        assert c.pause in PauseSemantics, f"bad pause on {method} {_path}"


def test_keyed_effect_contracts_require_business_transaction_ownership() -> None:
    """A response cache cannot justify ambiguous retries of business effects."""
    for (method, path), contract in contracts.ROUTE_CONTRACTS.items():
        if contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY:
            assert contract.transactional_idempotency, (method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("PATCH", "/api/agents/7/notices/current/guarded-v1"),
        ("POST", "/api/agents/7/notices/current/dismiss/guarded-v1"),
        ("POST", "/api/keyed/v1/agents/7/notices/11/resolve"),
    ],
)
def test_guarded_notice_contract_excludes_legacy_retry(method: str, path: str) -> None:
    contract = contracts.contract_for(method, path)
    assert contract is not None
    assert contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY
    assert contract.transactional_idempotency
    assert not contract.legacy_keyed_retry
