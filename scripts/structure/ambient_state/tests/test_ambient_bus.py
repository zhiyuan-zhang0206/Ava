"""The ambient-bus rule (scripts/structure/ambient_state/busrule.py)."""

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
    monkeypatch.setattr(allow, "BUS_PACKAGES", {_PACKAGE: frozenset({_ROOT})})


def _sites(source: str, rel: str = f"{_PACKAGE}/core.py") -> dict[str, int]:
    tree = ast.parse(textwrap.dedent(source))
    measured = ambient_state.measure(tree, rel, pathlib.Path("/nonexistent"))
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


@pytest.mark.parametrize(
    "imports",
    [
        "from base.events.live.bus import EventBus",
        "from base.events.live.bus import EventBus as Bus",
        "from base.events.live import EventBus",
    ],
)
def test_building_the_bus_outside_a_root_is_a_site(imports: str) -> None:
    alias = "Bus" if " as Bus" in imports else "EventBus"
    source = f"{imports}\n\n\ndef f():\n    return {alias}.from_settings()\n"
    assert _sites(source) == {"ambient-bus:EventBus.from_settings": 1}


def test_a_bus_taken_from_the_root_is_not_a_site() -> None:
    source = """
        async def run(bus):
            client = bus.async_redis()
            await bus.publish_best_effort("p", context="t")
            return client, bus.channel
    """
    assert _sites(source) == {}


def test_the_root_may_build_the_bus() -> None:
    source = """
        from base.events.live.bus import EventBus

        def f():
            return EventBus.from_settings()
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
    source = "from base.events.live.bus import EventBus\n\n\ndef f():\n    return EventBus.from_settings()\n"
    assert _sites(source, rel) == {}


def test_a_listed_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "BUS_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "BUS_" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
