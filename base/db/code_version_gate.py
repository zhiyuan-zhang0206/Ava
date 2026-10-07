"""The client-side code-version gate on pooled Postgres sessions.

A runner that is offline during an update (a closed laptop) is not stopped and
its checkout does not move. When it wakes, its already-running processes keep
writing with the old code. The gate closes that hole: `deployment_state` holds
`min_code_version`, every gateway start raises it to that gateway's own code
version (`raise_min_code_version`), and every pooled session reads it and
refuses to work under a lower one.

The check rides the baseline-session restore that every pooled borrow already
runs (`base.db.connections._restore_pooled_session`): the read of the minimum is
one more column of the restore's second statement, so it adds no round trip, and
a process-local timestamp limits the read to once per `MIN_REFRESH_INTERVAL_S`.

A stale process terminates itself, and does not raise. `psycopg_pool` swallows
an `Exception` from a `check` or `configure` callback and retries until
`PoolTimeout`, and `configure` runs on a worker thread that no caller sees.
`os._exit` acts from any thread and gives the old code no chance to keep
writing during a graceful shutdown. The gate trusts the process it stops to
run this code: a process that predates the gate cannot be stopped by it (the
first release carrying it cannot deliver its own protection), and a process
holding one connection forever never re-reads the minimum. The trade-off is in
`docs/decisions/runtime/updates/execution/converge/2026-09-30-client-side-code-version-gate.md`.

Direct connections (administrator, migration applier, `pg_dump`) and explicit
targets (`connect_url`) are not gated. The `ava` CLI is exempt by declaration
(`base.native_process.code_version.exempt_from_db_gate`): `ava stop` writes to
drain agents, and a host left behind must still be able to run it.
"""

from __future__ import annotations

import sys
import time
from typing import Any, NoReturn, Protocol, cast

import psycopg

from base.agents.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE
from base.log import logger
from base.native_process import code_version

# How stale the process-local copy of `min_code_version` may be. It bounds how
# long a process keeps writing after the minimum rises; it is not a lattice clock.
MIN_REFRESH_INTERVAL_S = 30.0

# time.monotonic() of the last read. Only the moment is kept, not the value: the
# version of a process never changes and the minimum only rises, so a read that
# passed can never make a later comparison fail without a fresh read.
_last_read_at: float | None = None


def min_read_due() -> bool:
    """Whether the restore should read `min_code_version` this time."""
    if not code_version.db_gate_applies():
        return False
    seen = _last_read_at
    return seen is None or time.monotonic() - seen >= MIN_REFRESH_INTERVAL_S


def observe_minimum(minimum: int) -> None:
    """Note that the cluster minimum was just read; terminate this process if its
    code version is below it."""
    global _last_read_at  # noqa: PLW0603 — process-lifetime cache, one per process by design
    _last_read_at = time.monotonic()
    version = code_version.get()
    if version < minimum:
        _refuse(version, minimum)


_REFUSAL = (
    "code version gate: process {service} runs code version {version}, below the "
    "cluster minimum {minimum} (deployment_state.min_code_version). This is stale "
    "code and must not write; exiting with code {code}. Update this host's checkout "
    "to the cluster's current commit. After an intentional rollback, lower "
    "min_code_version by hand (docs/conventions/runbook.md, Code version gate)."
)


def _has_log_sink() -> bool:
    """Whether loguru has a handler, i.e. whether a record written now goes anywhere."""
    return bool(cast(Any, logger)._core.handlers)  # private `_core`, as `base.log` reads it


def _refuse(version: int, minimum: int) -> NoReturn:
    from base.daemon.shutdown import hard_exit
    from base.telemetry import process_name

    fields = {
        "service": process_name(),
        "version": version,
        "minimum": minimum,
        "code": CODE_BEHIND_MINIMUM_EXIT_CODE,
    }
    logger.critical(_REFUSAL, **fields)
    if not _has_log_sink():
        # A process that dials before it opens its sinks would otherwise exit
        # without a word: loguru discards a record no handler receives.
        sys.stderr.write(_REFUSAL.format(**fields) + "\n")
    hard_exit(CODE_BEHIND_MINIMUM_EXIT_CODE)


def application_name() -> str:
    """The `application_name` a pooled connection carries: `ava:<process>:v<version>`.

    PgBouncer's `SHOW CLIENTS` and `pg_stat_activity` then show which process,
    on which code version, holds each connection. An exempt process (the CLI)
    carries no version: it never enforces one, so it must not need a git
    checkout just to dial.

    Raises:
        CodeVersionError: this process must carry a version and cannot resolve it.
    """
    from base.telemetry import process_name

    if not code_version.db_gate_applies():
        return "ava:cli"
    return f"ava:{process_name()}:v{code_version.get()}"


class _Dialer(Protocol):
    """What `raise_min_code_version` needs of a `Database`; the handle's own module imports this one."""

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection: ...


def raise_min_code_version(db: _Dialer) -> int:
    """Raise the cluster minimum to this process's code version; return the minimum.

    Called once by every gateway start, after migrations and the schema
    assertion. `GREATEST` keeps a rollback from lowering it: going back to older
    code leaves the minimum where it was, so the older code refuses to write
    until an operator lowers it by hand.

    Raises:
        CodeVersionError: this process has no resolvable code version.
        RuntimeError: the `deployment_state` singleton row is missing.
    """
    version = code_version.get()
    with db.connect(autocommit=True) as conn:
        row = conn.execute(
            "UPDATE deployment_state SET min_code_version = GREATEST(min_code_version, %s) "
            "WHERE id = 1 RETURNING min_code_version",
            (version,),
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "cannot raise min_code_version: the deployment_state singleton row is missing"
        )
    minimum = int(row[0])
    logger.info(
        "code version gate: cluster minimum is {minimum} (this gateway runs {version})",
        minimum=minimum,
        version=version,
    )
    return minimum
