"""Subprocess exit code constants, and the hard exit that leaves with one.

Standalone top-level module (not under meiva/) — because
meiva/__init__.py triggers `_MeivaEnv()` to read env and initialize
DB/Redis connections, which is only valid in a subprocess; importing
from an agent loop (agent/) process would blow up. Exit code constants
themselves have no env dependency; putting them at the top level lets
both sides import without triggering meiva SDK initialization side
effects.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import traceback
from collections.abc import Callable
from typing import NoReturn

IDLE_EXIT_CODE = (
    42  # `meiva.self.terminate()` / `meiva.self.restart()`: this turn ends (halted=True)
)
SYSTEM_HALT_EXIT_CODE = 43  # `meiva.self.compact(summary)`: agent altered control flow

# `meiva restart` refused BEFORE stopping anything: its
# validate-before-kill preflight failed, so the host is untouched and still serving.
# Read by the detached updater shell, which must recover a host that is DOWN
# (`meiva start`) and must NOT `meiva start` over one that is deliberately still up.
# Distinct from a generic failure (1) precisely because "may be down" is the safe
# reading of any *other* non-zero code.
RESTART_DECLINED_EXIT_CODE = 3

# `meiva start` / `meiva restart` ran every step successfully and launched this host's
# services, but at least one of them never passed its liveness probe within
# `base.deploy.progress_timeout.SERVICE_READY_TIMEOUT_S`. The status snapshot printed just
# before the exit names which (`cli.commands.probe`).
#
# Its own code rather than 1, because the two ask a program to do different things.
# 1 means a start STEP failed — converge, the data plane, migrations, the schema
# assertion, machine registration — so the host may have no services at all and
# nothing about it is trustworthy. This code means the sequence completed and the
# host is up but incompletely: retrying `meiva start` is idempotent and reasonable.
#
# It must NOT be 3: `RESTART_DECLINED_EXIT_CODE` means "nothing was stopped, host
# still serving -> do NOT start over it". A restart that stopped services and came
# back with one not serving is the opposite of that, and routing it into the
# decline branch would leave a half-down host untouched; the correct response to
# this code is the idempotent `meiva start`.
SERVICES_NOT_READY_EXIT_CODE = 4

# A service process found the cluster's `deployment_state.min_code_version` above
# its own code version and exited at once (`base.db.code_version_gate`): it is
# stale code that must not write. Its own code so an operator reading a unit's
# last exit tells "refused by the gate, update this host" apart from a crash. It
# is neither 3 (the host is still serving) nor 4 (the start sequence completed).
CODE_BEHIND_MINIMUM_EXIT_CODE = 78

HELPER_CHAIN_BROKEN_EXIT_CODE = 70


# ── Leaving the process ───────────────────────────────────────────────────────
#
# Leave the process at once after daemon-owned cleanup, without thread joins.
#
# The daemons with an explicit drain cancel and await remaining loop tasks (see
# `base.daemon.shutdown`) so ``run()``'s cleanup can finish, then call ``hard_exit``.
# They do not close ``asyncio.Runner``: its default-executor shutdown can wait up to
# CPython's 300-second ``THREAD_JOIN_TIMEOUT``, already the stop flow's entire
# budget, and interpreter teardown can join surviving workers without a bound.
# A hard exit skips only teardown after daemon-owned cleanup; it also skips the
# telemetry emitter's atexit drain (accepted for this path in task #4320), so logs
# are flushed here first.


def hard_exit(code: int) -> NoReturn:
    """Flush logs and exit immediately after daemon-owned async cleanup.

    A failing teardown step must not block the exit: it is reported on stderr and the
    remaining steps still run.
    """

    def close_loguru_sinks() -> None:
        from loguru import logger as _loguru

        _loguru.remove()  # closes (and so flushes) every sink

    def flush_stdout() -> None:
        sys.stdout.flush()

    def flush_stderr() -> None:
        sys.stderr.flush()

    steps: tuple[tuple[str, Callable[[], None]], ...] = (
        ("closing the loguru sinks", close_loguru_sinks),
        ("logging.shutdown", logging.shutdown),
        ("flushing stdout", flush_stdout),
        ("flushing stderr", flush_stderr),
    )
    for step, run in steps:
        try:
            run()
        except Exception:
            # Logging is closing here: stderr is the one report channel left. If stderr itself
            # is closed or broken there is nowhere to report to.
            with contextlib.suppress(OSError, ValueError, AttributeError):
                sys.stderr.write(
                    f"hard_exit: {step} failed; exiting anyway\n{traceback.format_exc()}"
                )
    os._exit(code)
