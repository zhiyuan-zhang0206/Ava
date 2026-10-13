"""Bounded stop waits and the Postgres escalation report, shared by the data-plane stop
(`maintenance_stop`) and the unit stop (`cli/commands/lifecycle/service_stop.py`).

They live under `data_plane` because the data-plane stop is the lower caller: the unit stop
imports them, never the reverse."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable

from base import telemetry
from base.cluster import postgres as owned_postgres
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.telemetry import EventPipeline
from cli.commands.data_plane.maintenance_stop_report import occupied_groups

# How long a normal stop's terminal closure waits between its HUP/TERM and the
# SIGKILL of whatever is left (docs/decisions/runtime/processes/shutdown/2026-09-28-stop-escalates-to-sigkill.md),
# and how long Postgres' immediate shutdown is given to finish at the data-plane stop: a job
# that handles TERM gets this long to clean up. The stop's own deadline caps it as well.
PROCESS_CLEANUP_WAIT_S = 10.0

# The SIGKILL leg's own bound at a normal stop: each wait inside a session kill,
# the wait for the killed sessions' hosts to end, and the least the closure
# evidence waits (it also gets the rest of the stop deadline); at the data-plane stop, how
# long the SIGKILL of what outlives the immediate shutdown is waited for.
# Every wait ends as soon as its processes are gone. The leg runs even when the
# grace spent the rest of the stop deadline — a stop that reached its terminal
# phase closes its terminals — so a stop can overrun its deadline by this
# bounded leg.
PROCESS_KILL_WAIT_S = 3.0


def deadline_after(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return value


def wait_for_exit(
    tracked: set[OwnedProcess],
    deadline: float,
    *,
    groups: tuple[int, ...] = (),
    escalate: Callable[[set[OwnedProcess]], None] | None = None,
) -> None:
    """Wait for `tracked` identities and `groups` to empty, within `deadline`.

    `escalate`, when given, is called once per iteration with the current
    living identities, AFTER the descendant re-capture: it may deliver further
    graceful signals to confirmed-owned identities (issue #2123 — a leader that
    exits without closing its children must not stall the stop until the
    deadline; the caller's escalator owns the ownership validation). Never
    escalates to SIGKILL and never certifies success while anything is alive.
    """
    while True:
        living = {identity for identity in tracked if identity.live()}
        occupied = occupied_groups(groups)
        if not living and not occupied:
            return
        for identity in living:
            retain_processes(tracked, capture_tree(identity))
        if escalate is not None:
            escalate(living)
        try:
            budget = remaining(deadline)
        except TimeoutError:
            raise TimeoutError(
                f"maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}; occupied process groups: {occupied}"
            ) from None
        time.sleep(min(0.05, budget))


def report_postgres_stop_escalation(
    escalation: owned_postgres.Escalation,
    notes: list[str] | None = None,
    *,
    producer: Callable[[], EventPipeline],
) -> None:
    """Report a Postgres shutdown that had to be ended by an immediate one.

    The owner (`base.cluster.postgres`) logs the escalation; this adds the
    operator line and the `postgres_stop_escalated` event. `notes`, when the
    caller owns a stop journal, collects the line for it
    (`_temporary_stop._finish_stop`); a leg that owns no journal passes
    nothing and still gets stderr and the event
    (docs/decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md).
    """
    killed = ", ".join(str(pid) for pid in escalation.killed) or "none"
    note = (
        f"postgres {escalation.detail}; ended by an immediate shutdown "
        f"(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        f"killed leftover processes: {killed}"
    )
    print(f"  ! {note}", file=sys.stderr, flush=True)
    telemetry.emit(
        "telemetry",
        "postgres_stop_escalated",
        producer=producer,
        level="error",
        source="stop",
        attributes={"detail": escalation.detail, "killed": list(escalation.killed)},
    )
    if notes is not None:
        notes.append(note)
