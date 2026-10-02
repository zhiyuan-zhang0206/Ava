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
_SHIM = "base.events.live.redis_client"


@pytest.fixture(autouse=True)
def _governed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(allow, "BUS_PACKAGES", {_PACKAGE: frozenset({_ROOT})})


def _sites(source: str, rel: str = f"{_PACKAGE}/core.py") -> dict[str, int]:
    tree = ast.parse(textwrap.dedent(source))
    measured = ambient_state.measure(tree, rel, pathlib.Path("/nonexistent"))
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


@pytest.mark.parametrize(
    ("imports", "call", "expected"),
    [
        (f"from {_SHIM} import get_async_redis", "get_async_redis()", "get_async_redis"),
        (f"from {_SHIM} import sync_redis as _s", "_s()", "sync_redis"),
        (f"import {_SHIM}", f"{_SHIM}.open_async_redis('u')", "open_async_redis"),
        (f"import {_SHIM} as rc", "rc.publish_best_effort('c', 'p')", "publish_best_effort"),
        (
            "from base.events.live import redis_client",
            "redis_client.publish_best_effort_sync('c', 'p')",
            "publish_best_effort_sync",
        ),
        (
            "from base.events.live.bus import EventBus",
            "EventBus.from_settings()",
            "EventBus.from_settings",
        ),
    ],
)
def test_ambient_redis_entries_in_a_governed_package_are_sites(
    imports: str, call: str, expected: str
) -> None:
    source = f"{imports}\n\n\ndef f():\n    return {call}\n"
    assert _sites(source) == {f"ambient-bus:{expected}": 1}


def test_a_bus_taken_from_the_root_is_not_a_site() -> None:
    source = """
        async def run(bus):
            client = bus.async_redis()
            await bus.publish_best_effort("p", context="t")
            return client, bus.channel
    """
    assert _sites(source) == {}


def test_the_root_may_build_the_bus_but_not_use_the_shim() -> None:
    source = f"""
        from base.events.live.bus import EventBus
        from {_SHIM} import sync_redis

        def f():
            EventBus.from_settings()
            return sync_redis()
    """
    assert _sites(source, _ROOT) == {"ambient-bus:sync_redis": 1}


def test_helpers_of_the_shim_module_that_take_their_target_explicitly_are_not_sites() -> None:
    source = f"""
        from {_SHIM} import open_sync_redis, retry_auth_failures_async

        def f(url):
            return open_sync_redis(url), retry_auth_failures_async
    """
    assert _sites(source) == {}


@pytest.mark.parametrize("rel", ["services/other/core.py", f"{_PACKAGE}/tests/test_core.py"])
def test_other_modules_and_tests_are_outside_the_rule(rel: str) -> None:
    source = f"from {_SHIM} import sync_redis\n\n\ndef f():\n    return sync_redis()\n"
    assert _sites(source, rel) == {}


def test_a_listed_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "BUS_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "BUS_" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
