"""Host-level deploy state — posture + updater lease (R1, Task #1021).

One row per machine in `host_deploy_state` answers two questions the old signals
answered with files, session probes and log mtimes:

- **posture** (`idle` / `paused` / `converging`) — replaces the `cluster_paused`
  file and `updating.flag`: `paused` is the static "this host is drained, waiting
  for an update" window (the gateway's Phase A fan-out), `converging` is the
  updater actually running on this host (its lease is live). The gateway's 503
  middleware reads this host's posture; the stranded-pause controller reads it
  to tell a real pause from a stranded one.
- **paused_at** — the moment the current pause window started: set when the
  posture enters `paused`, preserved through `converging`, cleared on `idle`.
  It is the updater-outcome reader's anchor (what the `cluster_paused` file's
  mtime used to be): `updated_at` cannot serve, because every transition inside
  the window bumps it. NULL when the host is not paused.
- **updater lease** (`updater_lease_expires_at`) — the updater process's
  liveness as a lease-expiry judgment, replacing "the updater log's mtime has
  not advanced" (stalled-updater controller, Phase-B poll).

The old signals were retired by the old-signal sweep (PR5): the `cluster_paused`
file and `updating.flag` are no longer written or read, and every consumer reads
this module's row. The always-up gate's file is now owned separately by
`shared.ui_update_state`: whole-cluster UI ownership must span local
pause/converge/start transitions and the complete Phase-B tail.

Layering: `shared` must not import `cli`/`gateway`, and this module is read by
the gateway middleware, the ops controllers, the updater and the gate — the
machine identity comes from `shared.machine`, the DB from `shared.db`.
"""

from __future__ import annotations

import contextlib
import dataclasses as _dataclasses
import datetime as _dt
import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import shared.db
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.machine import machine_name
from shared.paths import run_dir

_log = logging.getLogger("shared.host_deploy_state")

POSTURE_IDLE = "idle"
POSTURE_PAUSED = "paused"
POSTURE_CONVERGING = "converging"
_VALID_POSTURES = (POSTURE_IDLE, POSTURE_PAUSED, POSTURE_CONVERGING)

# How long a crashed updater's lease keeps the host reading as "converging"
# before the stalled-updater controller reaps it. The family's one no-progress
# definition, expressed directly (like `shared.cluster_lock.SETTLE_TTL_S`): a
# lease that expired sooner than the updater's own stall timeout would let a
# slow-but-alive updater be reaped mid-work, and two clocks disagreeing about
# "stopped making progress" are two chances to get that wrong. Registered in
# `shared/timing.py` as an equality constraint.
UPDATER_LEASE_TTL_S = NO_PROGRESS_TIMEOUT_S


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

    @property
    def updater_expired(self) -> bool:
        """Whether an updater lease armed during THIS pause window has run out.

        The provable-stop fact — and deliberately narrower than "the column holds a
        past timestamp", because that reading is wrong in the expensive direction.
        **Nothing clears the column on the way into a pause**: `set_posture` owns the
        posture alone, on purpose (a pause landing mid-rollout must not erase a live
        updater's claim — audit 2026-08-08 P2). So a run that ended without clearing
        leaves its expiry in the row indefinitely, and the NEXT update inherits it.

        That next update then has a window — between `pause_local_cluster` and its
        updater's first `touch_updater_lease`, which is a detached session spawn plus
        a Python cold start, seconds on a Windows host — where the row reads exactly
        like a host whose updater died. Both readers of this fact act on it: Phase B
        abandons the host as `POLL_STALLED` (prod, `win`, 2026-08-06 and 08-12: both
        rounds the host went on to converge minutes later on its own), and
        `ops.updater_reap._updater_hung` force-kills the session that was just
        spawned.

        A lease armed during this window expires at `armed + UPDATER_LEASE_TTL_S`, so
        `expires - TTL` dates the arming and anything armed before `paused_at` belongs
        to an earlier update. Both ends of that comparison are Postgres timestamps
        (`_LEASE_EXPIRY_SQL`, and `paused_at`'s `now()`), which is what makes the
        subtraction meaningful across two machines. A row that cannot be dated at all
        (no pause window) is not evidence either: undatable is "cannot tell", which
        both callers must read as "do not act" — one would kill a live updater, the
        other would strand a working host.

        **The TTL is read at compare time, not stored with the lease.** So a run
        armed under one `UPDATER_LEASE_TTL_S` and dated under a smaller one back-dates
        its arming by the difference, and could read as an earlier update's residue for
        one window after such a change. Nothing sets a non-default TTL today (the
        parameter on `touch_updater_lease` has no non-default caller), so the exposure
        is a future edit of the constant, and it self-clears on the next lease written
        under the new value — worth knowing before shrinking it, not worth storing a
        second column for.
        """
        if self.updater_lease_expires_at is None or self.updater_live:
            return False
        if self.paused_at is None:
            return False
        armed = self.updater_lease_expires_at - _dt.timedelta(seconds=UPDATER_LEASE_TTL_S)
        return armed >= self.paused_at


def read(machine: str | None = None) -> HostDeployState | None:
    """This host's (or `machine`'s) deploy-state row; None when no row exists yet.

    A missing row reads as idle — every consumer's default — so a host that has
    never been paused (or whose row the migration did not seed) behaves exactly
    like an unpaused one.
    """
    machine = machine or machine_name()
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, posture, updated_at, updater_lease_expires_at, paused_at, now() "
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


def read_all() -> dict[str, HostDeployState]:
    """Every machine's deploy-state row, keyed by machine name.

    The gateway's deploy-window signal 3 reads the roster this way (R1,
    Task #1021) instead of probing each host's ops server: the probe's
    `current_orchestration` field is a session-name judgment that dies with
    the very daemon that answers it (`ops` stops mid self-update), while the
    posture row is written by the pause and the updater's lease — both outside
    the restarted services — and survives the whole window. A machine with no
    row has never transitioned and reads as idle.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, posture, updated_at, updater_lease_expires_at, paused_at, now() "
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


# The lease's expiry, computed by the DB rather than by the writer. Every fact
# this row is *compared against* — `paused_at`, `updated_at`, and the `now()` the
# reader brings back — is stamped by Postgres, so the expiry has to be too: a
# lease written from the runner's clock and dated against the gateway's is a
# subtraction across two clocks, and the drift between them is not bounded by
# anything (a Windows host resuming from sleep before NTP converges is the shape
# that matters here). One source, no drift class to reason about.
_LEASE_EXPIRY_SQL = (
    "CASE WHEN %s::float8 IS NULL THEN NULL ELSE now() + make_interval(secs => %s::float8) END"
)


def _upsert(
    posture: str, *, lease_ttl_s: float | None, updated_at: _dt.datetime | None = None
) -> None:
    """Write this host's row (INSERT ... ON CONFLICT). `lease_ttl_s=None` means
    "clear the lease"; a value means "expire that many seconds from **the DB's
    now**" (`_LEASE_EXPIRY_SQL`).

    `paused_at` follows the posture, never the caller: entering `paused` stamps
    the pause moment, `idle` clears it, and every other posture (i.e.
    `converging` — updater entry / lease renewal) preserves it, because the
    pause window's anchor must survive the transitions inside the window."""
    machine = machine_name()
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_deploy_state "  # noqa: S608 — _LEASE_EXPIRY_SQL is a module constant
            "    (machine, posture, updater_lease_expires_at, paused_at, updated_at) "
            f"VALUES (%s, %s, {_LEASE_EXPIRY_SQL}, "
            "    CASE WHEN %s = 'paused' THEN now() ELSE NULL END, COALESCE(%s, now())) "
            "ON CONFLICT (machine) DO UPDATE SET posture = EXCLUDED.posture, "
            "    updater_lease_expires_at = EXCLUDED.updater_lease_expires_at, "
            "    paused_at = CASE WHEN EXCLUDED.posture = 'paused' THEN now() "
            "                    WHEN EXCLUDED.posture = 'idle' THEN NULL "
            "                    ELSE host_deploy_state.paused_at END, "
            "    updated_at = EXCLUDED.updated_at",
            (machine, posture, lease_ttl_s, lease_ttl_s, posture, updated_at),
        )


def _upsert_posture_only(posture: str) -> None:
    """Write the posture column and leave the updater lease untouched.

    The posture-only shape of the transition (see set_posture): a pause or
    unpause mid-rollout must not clear the updater's lease — the lease is the
    stalled-updater controller's liveness judgment, owned exclusively by
    touch_updater_lease / clear_updater_lease (audit 2026-08-08 P2)."""
    machine = machine_name()
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
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

    Called by the pause/unpause lifecycle (`ops.cluster_pause`) and the updater
    entry/exit. A DB write failure raises (the caller decides).

    Posture and the updater lease are orthogonal facts: this write leaves the
    lease column untouched, so a pause/unpause landing mid-rollout cannot
    silently clear the updater's liveness claim and let the stalled-updater
    controller reap a live update (audit 2026-08-08 P2). `touch_updater_lease`
    owns the lease column exclusively.
    """
    if posture not in _VALID_POSTURES:
        raise ValueError(f"invalid posture: {posture!r}")
    _upsert_posture_only(posture)


def touch_updater_lease(ttl_s: float = UPDATER_LEASE_TTL_S) -> None:
    """The updater's liveness claim: (re)arm this host's updater lease and enter
    `converging`. Called at the updater's start and (in the Python path) on a
    renewal timer; expiry is the stalled judgment.

    The expiry is computed by Postgres, not here (`_LEASE_EXPIRY_SQL`). This runs on
    the RUNNER and everything that judges the result runs on the gateway, so the
    writer's clock is the one clock that must not enter the arithmetic.
    """
    _upsert(POSTURE_CONVERGING, lease_ttl_s=ttl_s)


def clear_updater_lease() -> None:
    """The updater's voluntary exit: drop the lease and leave the posture alone —
    `unpause` / `ava start` owns the return to `idle`, and an update that
    already restarted into `idle` must not be stamped back to `converging` by
    the chain's tail clear. A crashed updater leaves the lease to expire on its
    TTL (and the posture at `converging`, which is what gates resurrection
    until the controller reaps it).
    """
    machine = machine_name()
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE host_deploy_state SET updater_lease_expires_at = NULL WHERE machine = %s",
            (machine,),
        )


def updater_lease_live(machine: str | None = None) -> bool:
    """Whether `machine`'s (default this host's) updater lease is unexpired."""
    state = read(machine)
    return state.updater_live if state is not None else False


# ── updater mutual-exclusion lock ────────────────────────────────────────────

# The updater lease above is a LIVENESS claim, not a mutex: two updater
# processes on one host can both hold live leases. That is exactly what
# happened on win 2026-08-11 (task #1181): the rollout's Phase-B updater raced
# a second self-update, both wrote the schtasks XML / deploy-state mirror
# concurrently, and converge failed with WinError 87 / 32 — the host stayed
# offline until the strays were killed and converge re-run. This is the
# mutual-exclusion half: one per-host lock file held with fcntl (POSIX) /
# msvcrt (Windows) for the updater's whole run. The OS releases it when the
# holder dies, so there is no stale-lock handling.
#
# flock / msvcrt lock per open-file-description, so a second acquire in the
# SAME process also contends — which keeps tests honest (a second acquire
# fails while the first is held).
_UPDATER_LOCK_NAME = "updater.lock"

# FDs of held locks — kept open (and thus locked) until release_updater_lock.
_updater_lock_fds: list[int] = []


def _updater_lock_path() -> Path:
    return run_dir() / _UPDATER_LOCK_NAME


def try_acquire_updater_lock() -> bool:
    """Take this host's updater mutex; False when another updater holds it.

    Non-blocking. Fail-soft by contract: a lock that cannot be taken because
    of a filesystem quirk must not abort an update the operator asked for, so
    only a genuine concurrent holder returns False. The caller releases with
    release_updater_lock() in its finally.
    """
    path = _updater_lock_path()
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte RANGE — initialize one byte only on first
            # creation. Never truncate the stable inode while another process
            # may have that range locked.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        _log.exception("[updater-lock] could not acquire %s", path)
        raise
    _updater_lock_fds.append(fd)
    return True


def release_updater_lock() -> None:
    """Drop the updater mutex while preserving its stable lock-file inode.

    Unlinking after close opens a split-inode race: one process can still hold
    the old inode while another creates and locks a new path. The 0600 file is
    intentionally permanent; only the advisory lock denotes ownership.
    """
    while _updater_lock_fds:
        fd = _updater_lock_fds.pop()
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
