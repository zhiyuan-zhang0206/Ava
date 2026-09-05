"""`shared.log._install_stdlib_intercept` — first-party DEBUG passthrough (#1126).

Bug: controllers' `_log.debug(...)` on stdlib `logging.getLogger(...)` loggers
(e.g. `ops.controllers.respawn`'s "gateway not healthy, deferring respawn"
line) never reached any sink. `logging.basicConfig(level=logging.INFO,
force=True)` set the stdlib root's floor to INFO, so a DEBUG record was
dropped at the *originating* logger — `isEnabledFor(DEBUG)` returned False —
before it ever reached `_StdlibInterceptHandler`, let alone loguru. 68 days of
`restarter.log` had zero occurrences of a line that fires every time the
respawn controller's gateway-health gate defers a restart.

Fix: `_install_stdlib_intercept` raises the effective level back to DEBUG for
`_FIRST_PARTY_LOGGER_NAMES` (our own top-level logger names) via stdlib's own
ancestor-level lookup, while the root stays at INFO — so third-party libraries
(httpx, psycopg, ...), which never call
`setLevel` on themselves, stay gated, and only our own code's DEBUG reaches
the (already level=DEBUG) file sink.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from shared.log import _FIRST_PARTY_LOGGER_NAMES, _install_stdlib_intercept


@pytest.fixture(autouse=True)
def _reset_touched_logger_levels() -> Iterator[None]:
    """`_install_stdlib_intercept` mutates process-global `logging` state
    (levels persist on the module-level `Logger.manager.loggerDict` singleton,
    independent of test order): it calls `.setLevel(DEBUG)` on each name in
    `_FIRST_PARTY_LOGGER_NAMES` directly — the probe loggers used below are
    *children* of those names and are never themselves leveled, they just
    inherit. Reset the actual mutated names after each test so this file
    doesn't leak DEBUG-everywhere into unrelated tests elsewhere in the
    suite."""
    yield
    for prefix in _FIRST_PARTY_LOGGER_NAMES:
        logging.getLogger(prefix).setLevel(logging.NOTSET)
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_first_party_controller_debug_reaches_loguru(loguru_records: list[dict]) -> None:
    """A DEBUG record from a stdlib logger under a first-party namespace
    (e.g. `ops.controllers.respawn`) now flows through to loguru — the exact
    shape of the dropped "gateway not healthy, deferring respawn" line."""
    _install_stdlib_intercept()
    logging.getLogger("ops.controllers.respawn").debug(
        "gateway not healthy, deferring respawn of %d agent(s)", 3
    )
    assert any(
        r["level"].name == "DEBUG" and "deferring respawn of 3 agent(s)" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records
    )


@pytest.mark.parametrize("prefix", _FIRST_PARTY_LOGGER_NAMES)
def test_every_first_party_prefix_passes_debug(prefix: str, loguru_records: list[dict]) -> None:
    """Every listed first-party namespace gets the same DEBUG passthrough —
    not just ops.controllers.*, but services / shared / gateway / agent /
    ava_builtins daemons and controllers."""
    _install_stdlib_intercept()
    logging.getLogger(f"{prefix}.some_module").debug("marker-%s", prefix)
    assert any(
        r["level"].name == "DEBUG" and f"marker-{prefix}" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records
    )


def test_third_party_logger_debug_still_gated(loguru_records: list[dict]) -> None:
    """A non-first-party stdlib logger (httpx stand-in) stays gated at DEBUG —
    the noise the INFO floor exists to keep out. A long-lived gateway.log
    already grew unbounded once from unfiltered volume (see
    `_add_file_sink`'s docstring); this locks in that the fix does not widen
    the floor for everyone, only for our own code."""
    _install_stdlib_intercept()
    logging.getLogger("httpx").debug("connection pool churn")
    assert not any("connection pool churn" in r["message"] for r in loguru_records)

    # Sanity: the floor is INFO, not OFF — the same logger's INFO still gets through.
    logging.getLogger("httpx").info("request completed")
    assert any("request completed" in r["message"] for r in loguru_records)
