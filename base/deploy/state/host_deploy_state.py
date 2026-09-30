"""Host-level deploy posture (R1, Task #1021).

One row per machine in `host_deploy_state` records that machine's **posture** —
`idle` or `paused`. `paused` is the window in which this host is stopped or held
by maintenance (`ava stop`, `ava pause`, `ava maintenance`); `ava start` returns
it to `idle`. The gateway's 503 middleware and `ava status` read this host's
posture, and `ops.deploy_window` reads every machine's row to tell a health-probe
alert that a transition is under way.

Layering: `base` must not import `cli`/`gateway`, and this module is read by
the gateway middleware, maintenance and the deploy-window signal — the machine
identity comes from `base.cluster.machine`, the DB from `base.db`.
"""

from __future__ import annotations

import dataclasses as _dataclasses
import datetime as _dt
from dataclasses import dataclass
from typing import Any

import base.db
from base.cluster.machine import machine_name
from base.db.transaction import write_transaction

POSTURE_IDLE = "idle"
POSTURE_PAUSED = "paused"
_VALID_POSTURES = (POSTURE_IDLE, POSTURE_PAUSED)


@dataclass(frozen=True)
class HostDeployState:
    """One host's deploy state row, as the DB sees it.

    **Every timestamp here is Postgres', including the "now" the age judgments
    compare against.** The row is written by one machine and read by another, so a
    comparison that takes either end from a local clock is a subtraction across two
    of them, and nothing bounds their disagreement — a host resuming from sleep
    before NTP converges is the shape that reaches minutes. `db_now` is selected in
    the same statement as the row (`read` / `read_all`) so the comparison has one
    source.

    The default is only for rows built by hand (tests, and a caller assembling a
    projection): such a row's timestamps are that process's own, so comparing them
    against that process's clock is the self-consistent reading.
    """

    machine: str
    posture: str
    updated_at: _dt.datetime
    db_now: _dt.datetime = _dataclasses.field(default_factory=lambda: _dt.datetime.now(_dt.UTC))


def _read_with_conn(conn: Any, machine: str) -> HostDeployState | None:
    """Read one host row through a connection whose lifecycle the caller owns."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT machine, posture, updated_at, now() FROM host_deploy_state WHERE machine = %s",
            (machine,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return HostDeployState(machine=row[0], posture=row[1], updated_at=row[2], db_now=row[3])


def read(machine: str | None = None, *, conn: Any | None = None) -> HostDeployState | None:
    """This host's (or `machine`'s) deploy-state row; None when no row exists yet.

    A missing row reads as idle — every consumer's default — so a host that has
    never been paused (or whose row the migration did not seed) behaves exactly
    like an unpaused one. When `conn` is supplied, the caller owns its lifecycle;
    otherwise this read opens and closes one connection as before.
    """
    machine = machine or machine_name()
    if conn is not None:
        return _read_with_conn(conn, machine)
    with base.db.connect(autocommit=True) as owned_conn:
        return _read_with_conn(owned_conn, machine)


def read_all() -> dict[str, HostDeployState]:
    """Every machine's deploy-state row, keyed by machine name.

    The deploy-window posture signal reads the roster this way (R1, Task #1021)
    instead of probing each host's ops server: the posture row is written outside
    the restarted services and survives the whole window, while an ops daemon
    stops with the services it would report on. A machine with no row has never
    transitioned and reads as idle.
    """
    with base.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT machine, posture, updated_at, now() FROM host_deploy_state")
        return {
            row[0]: HostDeployState(
                machine=row[0], posture=row[1], updated_at=row[2], db_now=row[3]
            )
            for row in cur.fetchall()
        }


def set_posture(posture: str) -> None:
    """Transition THIS host's posture (idle/paused).

    Called by the pause/unpause lifecycle (`ops.cluster_pause`) and the `ava
    start` tail. A DB write failure raises (the caller decides).
    """
    if posture not in _VALID_POSTURES:
        raise ValueError(f"invalid posture: {posture!r}")
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_deploy_state (machine, posture, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (machine) DO UPDATE SET posture = EXCLUDED.posture, "
            "    updated_at = EXCLUDED.updated_at",
            (machine_name(), posture),
        )
