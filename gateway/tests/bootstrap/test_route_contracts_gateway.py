"""Every gateway route declares a contract and no contract is orphaned."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from base.api_contracts import contracts
from gateway.app import app


def _iter_effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Yield leaf route entries from fastapi's effective route tree.

    fastapi >= 0.141 no longer yields ``APIRoute`` objects from ``app.routes``:
    each router include surfaces a wrapper whose ``effective_candidates()``
    nests until it reaches a leaf. Duck-typed (the wrapper classes are
    fastapi-private), and recursed because includes can nest.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if candidates is None:
            yield route
        else:
            yield from _iter_effective_routes(candidates())


def _app_route_keys() -> set[tuple[str, str]]:
    """(method, path template) for every HTTP route on the app."""
    keys: set[tuple[str, str]] = set()
    for route in _iter_effective_routes(app.routes):
        original = getattr(route, "original_route", route)
        path = getattr(route, "path", None) or getattr(original, "path", None)
        methods = getattr(original, "methods", None) or ()
        if not path:
            continue
        for method in methods:
            if method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                keys.add((method, path))
    return keys


def test_every_route_declares_a_contract() -> None:
    """Lint: no route may ship without a doorplate."""
    missing = _app_route_keys() - set(contracts.ROUTE_CONTRACTS)
    assert not missing, (
        "routes without a contract declaration — add them to "
        "base/api_contracts/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(missing))
    )


def test_no_orphan_contracts() -> None:
    """Lint: a declaration that no route uses is a lie — drop it."""
    orphan = set(contracts.ROUTE_CONTRACTS) - _app_route_keys()
    assert not orphan, (
        "contract declarations with no matching route — remove them from "
        "base/api_contracts/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(orphan))
    )
