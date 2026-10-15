from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from base.api_contracts import contracts
from tests.fixtures.gateway_config import gateway_test_app


def _iter_effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if candidates is None:
            yield route
        else:
            yield from _iter_effective_routes(candidates())


def _app_route_keys() -> set[tuple[str, str]]:
    app = gateway_test_app()
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
    missing = _app_route_keys() - set(contracts.ROUTE_CONTRACTS)
    assert not missing, (
        "routes without a contract declaration — add them to "
        "base/api_contracts/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(missing))
    )


def test_no_orphan_contracts() -> None:
    orphan = set(contracts.ROUTE_CONTRACTS) - _app_route_keys()
    assert not orphan, (
        "contract declarations with no matching route — remove them from "
        "base/api_contracts/contracts.py: " + ", ".join(f"{m} {p}" for m, p in sorted(orphan))
    )
