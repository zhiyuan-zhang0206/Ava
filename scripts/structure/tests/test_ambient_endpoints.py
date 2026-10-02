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
    ("imports", "call", "expected"),
    [
        ("from base.daemon.health import health_port", 'health_port("x")', "health_port"),
        ("from base.paths import pid_path as _pid", '_pid("x")', "pid_path"),
        ("import base.paths", 'base.paths.pid_path("x")', "pid_path"),
        ("import base.daemon.health as h", 'h.health_port("x")', "health_port"),
        ("from base import paths", 'paths.pid_path("x")', "pid_path"),
        ("from base.daemon import health", 'health.health_port("x")', "health_port"),
        (
            "from base.daemon.endpoints import ServiceEndpoints",
            "ServiceEndpoints.from_settings()",
            "ServiceEndpoints.from_settings",
        ),
    ],
)
def test_ambient_lookups_in_a_governed_package_are_sites(
    imports: str, call: str, expected: str
) -> None:
    source = f"{imports}\n\n\ndef f():\n    return {call}\n"
    assert _sites(source) == {f"ambient-endpoint:{expected}": 1}


def test_a_row_taken_from_the_root_is_not_a_site() -> None:
    source = """
        def run(endpoint):
            return endpoint.health_port, endpoint.pidfile, endpoint.healthz_url()
    """
    assert _sites(source) == {}


def test_the_root_may_build_the_table_but_not_look_up_ambiently() -> None:
    source = """
        from base.daemon.endpoints import ServiceEndpoints
        from base.paths import pid_path

        def f():
            ServiceEndpoints.from_settings()
            return pid_path("x")
    """
    assert _sites(source, _ROOT) == {"ambient-endpoint:pid_path": 1}


@pytest.mark.parametrize("rel", ["services/other/core.py", f"{_PACKAGE}/tests/test_core.py"])
def test_other_modules_and_tests_are_outside_the_rule(rel: str) -> None:
    source = 'from base.paths import pid_path\n\n\ndef f():\n    return pid_path("x")\n'
    assert _sites(source, rel) == {}


def test_a_listed_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "ENDPOINT_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "ENDPOINT" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
