"""The ambient-endpoint rule (scripts/structure/ambient_state/endpointrule.py)."""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from scripts.structure import ambient_state
from scripts.structure.ambient_state import allowlist as allow

_PACKAGE = "services/thing"
_ROOT = f"{_PACKAGE}/daemon.py"


@pytest.fixture(autouse=True)
def _governed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(allow, "ENDPOINT_PACKAGES", {_PACKAGE: frozenset({_ROOT})})


def _sites(source: str, rel: str = f"{_PACKAGE}/core.py") -> dict[str, int]:
    tree = ast.parse(textwrap.dedent(source))
    measured = ambient_state.measure(tree, rel, pathlib.Path("/nonexistent"))
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


@pytest.mark.parametrize(
    "imports",
    [
        "from base.daemon.endpoints import ServiceEndpoints",
        "from base.daemon.endpoints import ServiceEndpoints as Table",
    ],
)
def test_building_the_table_outside_a_root_is_a_site(imports: str) -> None:
    alias = "Table" if imports.endswith("Table") else "ServiceEndpoints"
    source = f"{imports}\n\n\ndef f():\n    return {alias}.from_settings()\n"
    assert _sites(source) == {"ambient-endpoint:ServiceEndpoints.from_settings": 1}


def test_a_row_taken_from_the_root_is_not_a_site() -> None:
    source = """
        def run(endpoint):
            return endpoint.health_port, endpoint.pidfile, endpoint.healthz_url()
    """
    assert _sites(source) == {}


def test_the_root_may_build_the_table() -> None:
    source = """
        from base.daemon.endpoints import ServiceEndpoints

        def f():
            return ServiceEndpoints.from_settings().of("x")
    """
    assert _sites(source, _ROOT) == {}


def test_an_unrelated_from_settings_is_not_a_site() -> None:
    source = """
        from base.db import Database

        def f():
            return Database.from_settings()
    """
    assert _sites(source) == {}


@pytest.mark.parametrize("rel", ["services/other/core.py", f"{_PACKAGE}/tests/test_core.py"])
def test_other_modules_and_tests_are_outside_the_rule(rel: str) -> None:
    source = "from base.daemon.endpoints import ServiceEndpoints\n\n\ndef f():\n    return ServiceEndpoints.from_settings()\n"
    assert _sites(source, rel) == {}


def test_a_listed_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "ENDPOINT_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "ENDPOINT" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
