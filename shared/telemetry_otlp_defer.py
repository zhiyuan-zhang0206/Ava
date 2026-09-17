"""Child OTLP deferral policy — hold exec-child records until the backend is
deliberately brought up (task #3816 M4b).

An exec child arms the deferral before its first record; batches are held in
the OTLP backend's bounded queue with the OTel stack, the settings chain, and
the metric plumbing unimported, and complete at:

- ``complete("finalize")`` — clean exit, or the shutdown fallback on abnormal
  exits: the hold is brought up and drained synchronously;
- ``complete("saturation")`` — the hold's bound was reached: the child's own
  event volume says the exporter stack is worth its memory;
- ``complete("age")`` — the one-shot max-age clock, the observability-delay
  bound for a low-traffic long child (default 60 s).

Record values, attributes, and timestamps are unchanged by the delay
(timestamps come from the Event, not the export moment); the JSONL mirror is
unaffected, so a child's live local channel stays live. Completion order:
snapshot the hold BEFORE bring-up, replay metrics for exactly that snapshot
BEFORE the events become visible to the worker — and only after a successful
bring-up, so a failed attempt (which keeps the hold untouched under the
standard five-minute retry gate) cannot double-count when a later one succeeds.

Both knobs are read from the environment directly: reading them through the
settings singleton would import the full config chain this deferral exists to
keep out of the child's life. They stay registered as Settings fields
(``shared/config/observability.py``) so the .env surface, scope routing, and
docs have one home.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
from collections.abc import Callable

from shared.telemetry import Event

_CHILD_DEFER_ENV = "AVA_TELEMETRY_OTLP_CHILD_DEFER"
_CHILD_DEFER_MAX_AGE_ENV = "AVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S"
# Fallback when the env value is absent/unparseable/non-positive — mirrors the
# declared field default (locked by
# tests/shared/test_telemetry_otlp_child_defer.py::test_env_defaults_locked_to_settings_fields).
CHILD_DEFER_MAX_AGE_DEFAULT_S = 60.0
_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, *, default: bool) -> bool:
    """Tristate env read: explicit true/false spellings win; anything else
    (absent, empty, unrecognized) falls back to `default`."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE_SPELLINGS:
        return True
    if raw in _FALSE_SPELLINGS:
        return False
    return default


def _env_seconds(name: str, default: float) -> float:
    """Positive-float env read; absent/unparseable/non-positive -> default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class ChildDeferral:
    """The exec-child deferral state machine for one OTLP backend.

    The owner supplies the four capabilities the policy needs — so the
    decision logic lives here while the backend keeps its queue, bring-up,
    metrics, and live path: `bring_up()` (True when the backend is up),
    `record_metrics(event)`, `export_live(events)`, plus the shared bounded
    queue. The callables are looked up per call, so owner-level test seams
    (patching ``backend._ensure``) stay live.
    """

    def __init__(
        self,
        *,
        queue: queue.Queue[Event | None],
        bring_up: Callable[[], bool],
        record_metrics: Callable[[Event], None],
        export_live: Callable[[list[Event]], None],
    ) -> None:
        self._queue = queue
        self._bring_up = bring_up
        self._record_metrics = record_metrics
        self._export_live = export_live
        self._lock = threading.Lock()
        self.active = False
        self._clock_started = False
        self._clock: threading.Timer | None = None

    def arm(self) -> None:
        """Arm deferred export; no-op when AVA_TELEMETRY_OTLP_CHILD_DEFER is
        off (the documented revert switch)."""
        if self.active:
            return
        if not _env_flag(_CHILD_DEFER_ENV, default=True):
            return
        with self._lock:
            self.active = True

    def is_active(self) -> bool:
        return self.active

    def hold_batch(self, events: list[Event]) -> None:
        """Hold one deferred batch in the bounded queue (the worker is absent).

        Saturation is the per-child volume signal: this child's event volume
        outweighs the exporter stack, so complete the deferral and hand the
        remainder to the live path — shedding only when the bring-up itself
        failed, matching the live path's semantics for an unreachable
        collector. The max-age clock starts on the first held batch."""
        remaining: list[Event] = []
        for index, event in enumerate(events):
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                remaining = events[index:]
                break
        else:
            self._start_clock()
            return
        self.complete("saturation")
        if self.active:
            return  # bring-up failed: hold intact, the remainder degrades as live
        self._export_live(remaining)

    def complete(self, _reason: str) -> None:
        """Leave the deferral: bring the backend up and replay the hold.

        Serialized by the instance lock — saturation (drain thread), the
        max-age timer, and finalize() (exit thread) can race; the first
        entrant wins. Order matters (see the module docstring): snapshot the
        hold BEFORE bring-up, replay metrics for exactly that snapshot BEFORE
        the events become visible to the worker."""
        with self._lock:
            if not self.active:
                return
            self._cancel_clock()
            snapshot = self._take_hold()
            if not snapshot:
                self.active = False
                return
            if not self._bring_up():
                self._put_back(snapshot)
                return
            self.active = False
            for event in snapshot:
                with contextlib.suppress(Exception):
                    self._record_metrics(event)
            for event in snapshot:
                try:
                    self._queue.put_nowait(event)
                except queue.Full:  # pragma: no cover — snapshot <= queue bound
                    break

    def _start_clock(self) -> None:
        """Start the one-shot max-age timer on the first held batch.

        This is the observability-delay bound: a low-traffic long child must
        not stall its records until exit — at the configured age the stack
        comes up and the backlog ships. A child that exits earlier never fires
        it."""
        if self._clock_started:
            return
        self._clock_started = True
        age_s = _env_seconds(_CHILD_DEFER_MAX_AGE_ENV, CHILD_DEFER_MAX_AGE_DEFAULT_S)
        timer = threading.Timer(age_s, self.complete, args=("age",))
        timer.daemon = True
        self._clock = timer
        timer.start()

    def _cancel_clock(self) -> None:
        clock = self._clock
        if clock is not None:
            clock.cancel()

    def _take_hold(self) -> list[Event]:
        """Drain the queue's held events (the deferral's backlog)."""
        held: list[Event] = []
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return held
            if event is None:  # pragma: no cover — shutdown finalizes first
                break
            held.append(event)
        return held

    def _put_back(self, held: list[Event]) -> None:
        """Return an untouched hold after a failed bring-up (bounded, so the
        puts cannot overflow the queue that held them)."""
        for event in held:
            with contextlib.suppress(queue.Full):  # pragma: no cover — same bound
                self._queue.put_nowait(event)
