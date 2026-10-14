"""Read-only age check on this home's maintenance hold.

A hold (`deploy-pause-owner.json`, taken by `ava stop`/`ava restart`, released by
`ava start` after readiness) freezes every wake on its machine and never expires.
One left behind by a lost CLI session would otherwise go unnoticed. This
observation reports a hold older than a configured age; it never releases,
restarts or repairs anything.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from base.daemon.health import DaemonProbe
from base.deploy.maintenance import pause_owner

_RECOVERY = (
    "run `ava start` on this machine: it brings the services up and releases the hold once "
    "they are ready (a stop or restart still in progress is expected to hold it)"
)
# No command clears a journal without a readable hold; see graceful-maintenance.md.
_RECOVERY_UNREADABLE = (
    "confirm no `ava stop` is in flight, remove `run/deploy-pause-owner.json` under the "
    "home by hand, then run `ava start`"
)


@dataclass(frozen=True, slots=True)
class _Overdue:
    """One over-age hold as the last sample saw it; ``key`` names the hold, not the sample."""

    key: tuple[str, str | None, float]
    state: str
    holder: str | None
    phase: str | None
    acquired_at: str | None
    held_seconds: float


class MaintenanceHoldWatch:
    """The `maintenance-hold` diagnostic: a probe and its once-per-hold reporter.

    The monitor runs `probe` and then `report` inside one bounded worker pass, so
    the sample `report` acts on is the one `probe` just took.
    """

    def __init__(self, *, alert_after_s: float, clock: Callable[[], float] = time.time) -> None:
        if alert_after_s <= 0:
            raise ValueError("maintenance hold alert age must be positive")
        self._alert_after_s = alert_after_s
        self._clock = clock
        self._overdue: _Overdue | None = None
        self._reported: tuple[str, str | None, float] | None = None

    def probe(self) -> DaemonProbe:
        """UP unless a hold, or an unreadable journal, is older than the alert age."""
        self._overdue = None
        snapshot = pause_owner.read()
        now = self._clock()
        if snapshot.status in ("inactive", "resumed"):
            return DaemonProbe.up("no maintenance hold")
        if snapshot.status == "invalid":
            # Nothing names the hold's start, so the journal's own age stands in for it.
            since = pause_owner.state_path().stat().st_mtime
            overdue = _Overdue(("invalid", None, since), "invalid", None, None, None, now - since)
        else:
            assert snapshot.holder is not None and snapshot.acquired_at is not None  # noqa: S101
            assert snapshot.maintenance is not None  # noqa: S101
            overdue = _Overdue(
                ("paused", snapshot.holder, snapshot.acquired_at.timestamp()),
                "paused",
                snapshot.holder,
                snapshot.maintenance.phase.value,
                snapshot.acquired_at.isoformat(),
                now - snapshot.acquired_at.timestamp(),
            )
        if overdue.held_seconds < self._alert_after_s:
            return DaemonProbe.up(f"maintenance hold held for {overdue.held_seconds:.0f}s")
        self._overdue = overdue
        return DaemonProbe.down(
            f"maintenance {overdue.state} journal in place for {overdue.held_seconds:.0f}s "
            f"(alert age {self._alert_after_s:.0f}s)"
        )

    def report(self, result: DaemonProbe) -> None:
        """One event per hold: a healthy sample ends the episode and rearms the next."""
        if result.alive:
            self._reported = None
            return
        overdue = self._overdue
        if overdue is None or overdue.key == self._reported:
            return
        self._reported = overdue.key
        _emit_overdue(overdue)


def _emit_overdue(overdue: _Overdue) -> None:
    from base.log import logger

    logger.error(
        "[maintenance-hold healthcheck] the local maintenance hold ({state}; holder {holder}, "
        "phase {phase}) has been in place for {held_seconds:.0f}s and keeps every wake on this "
        "machine frozen; to recover, "
        + (_RECOVERY_UNREADABLE if overdue.state == "invalid" else _RECOVERY),
        event="maintenance_hold_overdue",
        state=overdue.state,
        holder=overdue.holder,
        phase=overdue.phase,
        acquired_at=overdue.acquired_at,
        held_seconds=overdue.held_seconds,
    )
