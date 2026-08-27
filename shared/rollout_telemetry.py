"""Rollout phase telemetry — every phase's duration, and the bytes it moved.

Task #1820 (user forensic ruling 2026-08-27): a migration-bearing rollout took
368s and the breakdown was only reconstructable by hand afterwards — the
pre-update snapshot (~148s), Phase 0's git fetch (69s), Phase B (77s) and the
page-server stop (15s) were each visible in the log text, but no single place
recorded them as numbers. This module makes each phase self-measuring.

Two line shapes, both parseable by grep and by a reader:

- per-stage, printed the moment the stage ends (so a rollout that dies
  mid-way still shows every stage it completed):

      [rollout-telemetry] stage=phase0_fetch dur=69.2s
      [updater] stage=uv dur=40.1s

- one aggregate JSON line at the end of the gateway orchestration:

      [rollout-telemetry] {"bytes": {"snapshot": 4567030217},
                           "hosts": {"win": {"uv": 40.1, "stop": 2.3}},
                           "stages": {"phase0_fetch": 69.2, ...},
                           "total_s": 368.1}

The per-stage lines are unconditional — `stage()` prints whether or not a
collector is active, so a helper called from a non-rollout context (a rollback,
a frontend-only fast path) still reports its own duration. The collector is an
ambient module global: the gateway orchestration calls `activate()` once and
every nested stage in the helper modules records into it without threading a
parameter through the call graph (safe because one orchestration is one
synchronous process).

The updater side (agent-runner self-update, `ava restart` behind the Windows
cmd.exe ladder) prints `[updater] stage=...` lines instead; `ops.updater_outcome`
parses them into `UpdaterOutcome.stages`, so per-host stage times travel with
the status probe and appear in the rollout report.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from contextlib import contextmanager


# Ambient collector for the duration of one gateway orchestration. Only ever
# set on the rollout process; a None collector leaves `stage()` a pure printer.
# A plain module-global slot rather than a contextvar: the orchestration is one
# synchronous process with no concurrent scopes to isolate, and the repo's
# contextvars allowlist (TID251, audit #3607) is for mechanism-layer runtime
# propagation — a CLI telemetry collector has no reason to join it.
class _CollectorSlot:
    """Mutable holder so `activate()`/`deactivate()` need no `global` statement."""

    def __init__(self) -> None:
        self.value: RolloutTelemetry | None = None


_active = _CollectorSlot()


class RolloutTelemetry:
    """Collector of rollout phase durations + byte counts for one orchestration.

    Durations are recorded as elapsed wall time around each phase (`stage`).
    Byte counts are recorded explicitly by the phases that move data (the
    pre-update data snapshot). `print_summary` emits the aggregate JSON line
    the rollout log ends with.
    """

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._bytes: dict[str, int] = {}
        self._hosts: dict[str, dict[str, float]] = {}
        self._started = time.monotonic()

    def record(self, name: str, dur_s: float) -> None:
        self._stages[name] = round(dur_s, 1)

    def record_bytes(self, name: str, n: int) -> None:
        self._bytes[name] = n

    def record_host(self, host: str, stages: dict[str, float]) -> None:
        """Per-host updater stage times, as reported by that host's status probe
        during the Phase-B poll (best-effort: a host that converged before its
        next probe answered reports nothing)."""
        if stages:
            self._hosts[host] = stages

    def total_s(self) -> float:
        return round(time.monotonic() - self._started, 1)

    def summary(self) -> dict[str, object]:
        return {
            "stages": self._stages,
            "bytes": self._bytes,
            "hosts": self._hosts,
            "total_s": self.total_s(),
        }

    def print_summary(self) -> None:
        """One machine-readable JSON line naming every phase + the total."""
        print(f"[rollout-telemetry] {json.dumps(self.summary(), sort_keys=True)}")  # noqa: T201


def activate() -> RolloutTelemetry:
    """Make `stage()` / `record_bytes()` / `record_host()` record into a fresh
    collector, and return it. Call once per gateway orchestration; the process
    is short-lived, so nothing ever needs to deactivate it."""
    collector = RolloutTelemetry()
    _active.value = collector
    return collector


def deactivate() -> None:
    """Drop the ambient collector (test teardown; the orchestration process
    exits instead)."""
    _active.value = None


def record_bytes(name: str, n: int) -> None:
    """Record a byte count (e.g. the pre-update snapshot's dump size) into the
    ambient collector, when one is active."""
    if _active.value is not None:
        _active.value.record_bytes(name, n)


def record_host(host: str, stages: dict[str, float]) -> None:
    """Record one host's updater stage times into the ambient collector."""
    if _active.value is not None:
        _active.value.record_host(host, stages)


@contextmanager
def stage(name: str) -> Generator[None, None, None]:
    """Time one gateway-orchestration phase.

    Prints `[rollout-telemetry] stage=<name> dur=..s` the moment the phase ends
    (so a killed rollout still shows what it completed), and records the same
    duration into the ambient collector when one is active.

    Nested stages are fine: the local leg's `stop` / `checkout` / `uv_sync` /
    `start` record beside the `local_leg` total that contains them.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        dur_s = time.monotonic() - started
        if _active.value is not None:
            _active.value.record(name, dur_s)
        print(f"[rollout-telemetry] stage={name} dur={dur_s:.1f}s")  # noqa: T201


@contextmanager
def updater_stage(name: str) -> Generator[None, None, None]:
    """Time one agent-runner updater stage (checkout / uv_sync / skills /
    quiesce / stop / start / preflight).

    Prints `[updater] stage=<name> dur=..s` on exit — the line
    `ops.updater_outcome._parse_stages` reads back off the updater log, so
    per-host stage times reach the rollout report. Printed unconditionally,
    because the updater side has no ambient collector.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        dur_s = time.monotonic() - started
        print(f"[updater] stage={name} dur={dur_s:.1f}s")  # noqa: T201
