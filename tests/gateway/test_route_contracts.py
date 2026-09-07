"""Doorplate lint (R3 doors ① + ②): the contract wall is machine-supervised.

Every gateway route must declare a contract (`shared/contracts.py` — the
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
from fastapi.routing import APIRoute

from gateway import _pause_policy
from gateway.app import app
from shared import contracts
from shared.contracts import Idempotency, PauseSemantics

# The audited exempt surface: exactly the surfaces that must stay reachable
# mid-migration. Adding a route here is a deliberate control-plane decision
# (it survives a migration and skips the pause 503); anything else is
# data-plane by default.
_EXPECTED_CONTROL_PLANE = frozenset(
    {
        ("POST", "/api/cluster/stop"),
        ("POST", "/api/cluster/resume"),
        ("POST", "/api/cluster/recover"),
        ("POST", "/api/cluster/stopping"),
        ("POST", "/api/cluster/update"),
        ("POST", "/api/cluster/rollout"),
        ("POST", "/api/cluster/restart"),
        ("GET", "/api/cluster/update-check"),
        ("GET", "/api/cluster/status"),
        ("GET", "/api/cluster/roster"),
        ("GET", "/api/cluster/admin/events"),
        ("GET", "/api/cluster/machines"),
        ("DELETE", "/api/cluster/machines/{name}"),
        ("POST", "/api/cluster/machines/{name}/staging"),
        ("POST", "/api/cluster/machines/{name}/pause"),
        ("POST", "/api/cluster/machines/{name}/resume"),
        ("POST", "/api/alerts"),
        ("POST", "/api/work-failed"),
    }
)


def _app_route_keys() -> set[tuple[str, str]]:
    """(method, path template) for every HTTP route on the app."""
    keys: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                if method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    keys.add((method, route.path))
    return keys


def test_every_route_declares_a_contract() -> None:
    """Lint: no route may ship without a doorplate."""
    missing = _app_route_keys() - set(contracts.ROUTE_CONTRACTS)
    assert not missing, (
        "routes without a contract declaration — add them to "
        "shared/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(missing))
    )


def test_no_orphan_contracts() -> None:
    """Lint: a declaration that no route uses is a lie — drop it."""
    orphan = set(contracts.ROUTE_CONTRACTS) - _app_route_keys()
    assert not orphan, (
        "contract declarations with no matching route — remove them from "
        "shared/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(orphan))
    )


def test_pause_exempt_surface_is_audited() -> None:
    """The exempt surface is exactly the reviewed set — no silent additions.

    This is the "enumerable and auditable" property of doorplate ②: a new
    exemption must be a deliberate edit to this test + the contract, never
    an invisible middleware string.
    """
    assert _pause_policy.control_plane_surface() == _EXPECTED_CONTROL_PLANE


def test_should_bypass_pause_agrees_with_surface() -> None:
    """The decision function answers by the declared surface, on concrete
    request paths (templates must match real paths)."""
    exempt = [
        ("GET", "/api/cluster/status"),
        ("POST", "/api/cluster/update"),
        ("POST", "/api/alerts"),
    ]
    blocked = [
        ("GET", "/api/alerts"),  # same template as the exempt webhook, different method
        ("GET", "/api/alerts/stream"),
        ("GET", "/api/agents"),
        ("GET", "/api/agents/42/messages"),
        ("GET", "/api/agents/42"),
        ("GET", "/api/cluster/status/extra"),  # exact match, not prefix
        ("GET", "/api/health"),
        ("GET", "/pages/5-report/a/b"),
    ]
    for method, path in exempt:
        assert _pause_policy.should_bypass_pause(method, path), f"expected exempt: {method} {path}"
    for method, path in blocked:
        assert not _pause_policy.should_bypass_pause(method, path), (
            f"expected blocked: {method} {path}"
        )


def test_sdk_inherits_idempotency_from_contracts() -> None:
    """The three semantics resolve where the SDK looks them up."""
    assert contracts.idempotency_for("POST", "/api/agents") is Idempotency.NON_IDEMPOTENT
    assert (
        contracts.idempotency_for("POST", "/api/agents/7/messages")
        is Idempotency.AT_LEAST_ONCE_WITH_KEY
    )
    assert contracts.idempotency_for("GET", "/api/agents/7") is Idempotency.IDEMPOTENT
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
