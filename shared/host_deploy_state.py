"""Host-level deploy state — posture + updater lease (R1, Task #1021).

One row per machine in `host_deploy_state` answers two questions the old signals
answered with files, session probes and log mtimes:

- **posture** (`idle` / `paused` / `converging`) — replaces the `cluster_paused`
  file and `updating.flag`: `paused` is the static "this host is drained, waiting
  for an update" window (the gateway's Phase A fan-out), `converging` is the
  updater actually running on this host (its lease is live). The gateway's 503
  middleware reads this host's posture.
- **paused_at** — the moment the current pause window started: set when the
  posture enters `paused`, preserved through `converging`, cleared on `idle`.
  It is the updater-outcome reader's anchor (what the `cluster_paused` file's
  mtime used to be): `updated_at` cannot serve, because every transition inside
  the window bumps it. NULL when the host is not paused.
- **updater lease** (`updater_lease_expires_at`) — the retired updater's
  liveness claim, written by Postgres' clock. No current code arms or clears
  it; readers still honour a row an upgraded host carried over, so a live
  lease keeps reading as an in-flight update until it expires.

The old signals were retired by the old-signal sweep (PR5): the `cluster_paused`
file and `updating.flag` are no longer written or read, and every consumer reads
this module's row.

Layering: `shared` must not import `cli`/`gateway`, and this module is read by
the gateway middleware, maintenance and the deploy-window signal — the machine
identity comes from `shared.machine`, the DB from `shared.db`.
"""

from __future__ import annotations

import dataclasses as _dataclasses
import datetime as _dt
from dataclasses import dataclass
from typing import Any

import shared.db
from shared.db_transaction import write_transaction
from shared.machine import machine_name

POSTURE_IDLE = "idle"
POSTURE_PAUSED = "paused"
POSTURE_CONVERGING = "converging"
_VALID_POSTURES = (POSTURE_IDLE, POSTURE_PAUSED, POSTURE_CONVERGING)


@dataclass(frozen=True)
class HostDeployState:
    """One host's deploy state row, as the DB sees it.

    **Every timestamp here is Postgres', including the "now" the judgments below
    compare against.** The row is written by one machine (the runner's updater) and
    read by another (the gateway's poll and controllers), so a comparison that takes
    either end from a local clock is a subtraction across two of them, and nothing
    bounds their disagreement — a Windows host resuming from sleep before NTP
    converges is the shape that reaches minutes. `db_now` is selected in the same
    statement as the row (`read` / `read_all`) so the comparison has one source.

    The default is only for rows built by hand (tests, and a caller assembling a
    projection): such a row's timestamps are that process's own, so comparing them
    against that process's clock is the self-consistent reading.
    """

    machine: str
    posture: str
    updated_at: _dt.datetime
    updater_lease_expires_at: _dt.datetime | None
    paused_at: _dt.datetime | None = None
    db_now: _dt.datetime = _dataclasses.field(default_factory=lambda: _dt.datetime.now(_dt.UTC))

    @property
    def updater_live(self) -> bool:
        """Whether this host's updater lease is unexpired — the liveness judgment."""
        return (
            self.updater_lease_expires_at is not None
            and self.updater_lease_expires_at > self.db_now
        )


def _read_with_conn(conn: Any, machine: str) -> HostDeployState | None:
    """Read one host row through a connection whose lifecycle the caller owns."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT machine, posture, updated_at, updater_lease_expires_at, paused_at, "
            "now() "
            "FROM host_deploy_state WHERE machine = %s",
            (machine,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return HostDeployState(
        machine=row[0],
        posture=row[1],
        updated_at=row[2],
        updater_lease_expires_at=row[3],
        paused_at=row[4],
        db_now=row[5],
    )


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
    with shared.db.connect(autocommit=True) as owned_conn:
        return _read_with_conn(owned_conn, machine)


def read_all() -> dict[str, HostDeployState]:
    """Every machine's deploy-state row, keyed by machine name.

    The gateway's deploy-window posture signal reads the roster this way (R1,
    Task #1021) instead of probing each host's ops server: the posture row is
    written by the pause and the updater's lease — both outside the restarted
    services — and survives the whole window, while an ops daemon stops with the
    services it would report on. A machine with no row has never transitioned
    and reads as idle.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, posture, updated_at, updater_lease_expires_at, paused_at, "
            "now() "
            "FROM host_deploy_state"
        )
        return {
            row[0]: HostDeployState(
                machine=row[0],
                posture=row[1],
                updated_at=row[2],
                updater_lease_expires_at=row[3],
                paused_at=row[4],
                db_now=row[5],
            )
            for row in cur.fetchall()
        }


def _upsert_posture_only(posture: str) -> None:
    """Write the posture column and leave the updater lease untouched.

    The posture-only shape of the transition (see set_posture): a pause or
    unpause must not clear a retained updater lease — the readers treat a live
    one as an in-flight update (audit 2026-08-08 P2)."""
    machine = machine_name()
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_deploy_state (machine, posture, paused_at, updated_at) "
            "VALUES (%s, %s, "
            "    CASE WHEN %s = 'paused' THEN now() ELSE NULL END, COALESCE(%s, now())) "
            "ON CONFLICT (machine) DO UPDATE SET posture = EXCLUDED.posture, "
            "    paused_at = CASE WHEN EXCLUDED.posture = 'paused' THEN now() "
            "                    WHEN EXCLUDED.posture = 'idle' THEN NULL "
            "                    ELSE host_deploy_state.paused_at END, "
            "    updated_at = EXCLUDED.updated_at",
            (machine, posture, posture, None),
        )


def set_posture(posture: str) -> None:
    """Transition THIS host's posture (idle/paused/converging).

    Called by the pause/unpause lifecycle (`ops.cluster_pause`) and the `ava
    start` tail. A DB write failure raises (the caller decides).

    Posture and the updater lease are orthogonal facts: this write leaves the
    lease column untouched, so a pause/unpause cannot silently clear a retained
    updater's liveness claim (audit 2026-08-08 P2).
    """
    if posture not in _VALID_POSTURES:
        raise ValueError(f"invalid posture: {posture!r}")
    _upsert_posture_only(posture)


def updater_lease_live(machine: str | None = None) -> bool:
    """Whether `machine`'s (default this host's) updater lease is unexpired."""
    state = read(machine)
    return state.updater_live if state is not None else False
