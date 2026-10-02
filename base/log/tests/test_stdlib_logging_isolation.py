"""`tests/fixtures/guards.py::stdlib_logging_isolated` — no stdlib logging state outlives a test.

`base.log._install_stdlib_intercept()` (run for real by the intercept tests) sets the root
logger to INFO and every first-party logger to DEBUG. The autouse guard used to put the
root *handlers* back and nothing else, so those levels leaked into every later test in the
same xdist worker: a stdlib log call that a default-WARNING run never emits (its record is
dropped at the level gate before formatting) was formatted there, and a bad format string
in production code surfaced as a `TypeError` in an unrelated test — but only in the
worker layouts where an intercept test happened to run first.

The guard's teardown runs after a test body, where a self-test cannot observe it, so the
two halves are pinned separately: the guard is wired onto every test, and its one action
undoes a *real* `_install_stdlib_intercept()`.
"""

from __future__ import annotations

import logging

import pytest

from base.log import _FIRST_PARTY_LOGGER_NAMES, _install_stdlib_intercept, _StdlibInterceptHandler
from tests.fixtures.guards import stdlib_logging_isolated


def _all_levels() -> dict[str, int]:
    """Level of every logger that exists now, the root included."""
    levels = {"": logging.getLogger().level}
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger):
            levels[name] = logger.level
    return levels


def test_the_guard_is_wired_onto_every_test(request: pytest.FixtureRequest) -> None:
    assert "_no_stdlib_telemetry_bridge" in request.fixturenames


def test_a_real_intercept_install_is_undone_level_for_level() -> None:
    before = _all_levels()
    handlers_before = logging.getLogger().handlers[:]

    with stdlib_logging_isolated():
        _install_stdlib_intercept()
        # The leak is real: this is what the next test used to inherit.
        assert logging.getLogger().level == logging.INFO
        assert all(logging.getLogger(n).level == logging.DEBUG for n in _FIRST_PARTY_LOGGER_NAMES)
        assert any(isinstance(h, _StdlibInterceptHandler) for h in logging.getLogger().handlers)

    after = _all_levels()
    assert {name: level for name, level in after.items() if name in before} == before, (
        "a logger `_install_stdlib_intercept` sets is missing from "
        "`_INTERCEPT_LEVELED_THIRD_PARTY` in tests/fixtures/guards.py"
    )
    assert logging.getLogger().handlers == handlers_before


def test_a_first_party_logger_is_back_at_its_original_level_after_the_block() -> None:
    """The observable form of the leak: what a *later* test's ordinary logger sees."""
    probe = logging.getLogger("services.stdlib_logging_isolation_probe")
    original = probe.getEffectiveLevel()
    with stdlib_logging_isolated():
        _install_stdlib_intercept()
        assert probe.getEffectiveLevel() == logging.DEBUG
    assert probe.getEffectiveLevel() == original


def test_the_block_restores_on_an_exception() -> None:
    before = _all_levels()
    with pytest.raises(RuntimeError, match="boom"), stdlib_logging_isolated():
        _install_stdlib_intercept()
        raise RuntimeError("boom")
    assert {n: lv for n, lv in _all_levels().items() if n in before} == before


def test_the_bridge_handler_is_off_the_root_inside_the_block() -> None:
    root = logging.getLogger()
    bridge = _StdlibInterceptHandler()
    root.addHandler(bridge)
    try:
        with stdlib_logging_isolated():
            assert bridge not in root.handlers
        assert bridge in root.handlers
    finally:
        root.removeHandler(bridge)
