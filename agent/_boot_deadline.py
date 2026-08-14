"""The child's own boot watchdog: die when the boot stops making progress.

Everything an agent does before its `allocated -> starting` CAS is invisible from
outside. The row reads 'allocated' with no pid whether the child is halfway
through its import chain or died on the first import, so the launcher
(`ops/agent_launch.py`) could only ask the platform supervisor "does the process
still exist" — and a process that exists is not a process that is getting
anywhere. An alive-but-wedged child (a DB connect that black-holes, a lock never
acquired) held its row for the launcher's entire window and then some.

This module removes that ambiguity at the source, by making the child guarantee
the property the launcher wanted to check: **while this process is alive, its
boot is progressing.** A watchdog thread armed before the heavy imports watches
`agent/_boot_timing.py`'s phase marks; if no new phase is reached within
`stall_seconds`, it names the phase the boot died in and `os._exit`s. "Alive"
then means "progressing", and the launcher's existing liveness probe — which was
a proxy — becomes decisive.

Three properties make this work where a progress *report* would not:

- **Milestones, not a heartbeat.** The marks are points on the boot path, not a
  timer tick, so a wedged child cannot keep reassuring anyone: it stops emitting
  exactly when it stops progressing. A heartbeat thread would sail on past a
  deadlock and reproduce the unbounded patience this replaces.
- **The signal never leaves the process.** Nothing is written to a row this
  process does not own yet, no file, no socket. The one fact that crosses the
  boundary is whether the process exists, which the supervisor already answers.
  That also means the watchdog works during the segment a DB-backed progress
  column cannot reach: `shared.config` alone is ~half the pre-flip import cost,
  so any signal that must first import the DB stack reports only on the part of
  the boot that was never slow.
- **It bounds one phase, not the boot.** `stall_seconds` answers "how long may a
  single boot step take", which does not grow with import bloat or box load —
  unlike the wall-clock windows around it, which are guesses about how slow a
  loaded machine gets.

A second bound, `budget_seconds`, exists precisely because the first one bounds
nothing on its own: a boot that keeps reaching phases can live for
phases x stall, and that product moves the day someone adds a mark. Past
`allocated_reap_grace_seconds` the restarter's allocated-reaper takes an
'allocated' row on age alone — its clock is `status_changed_at`, which only a
status flip resets, so no amount of pre-flip progress holds it off. A boot that
outlived the grace would therefore have its row reaped out from under a live,
progressing child: the 2026-07-30 incident again, relocated from the launcher to
the reaper. Holding the budget below the grace means the reaper never meets a
live child, and the launcher resolves every launch.

The deadline is NOT self-reported to the database. A child that is wedged on the
data plane cannot be relied on to write to it — the write would hang in exactly
the case the watchdog exists for. Exiting is the one action that always
succeeds, and it lands the launcher on its fastest path: a dead process fails the
confirm immediately, which force-terminates the row with `termination_source =
'launch-confirm'` and hands it to crash-resurrect. The restarter's
allocated-reaper is unaffected and stays the backstop for a row whose process was
never launched at all.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from agent import _boot_timing

# How often the watchdog re-reads the phase count. Small next to any sane
# `stall_seconds`, so it costs at most this much extra patience, and a sleeping
# thread every half second is not measurable against an import chain.
_POLL_SECONDS = 0.5

_disarmed = threading.Event()

STALL_FLAG = "--boot-stall-seconds"
BUDGET_FLAG = "--boot-budget-seconds"


def consume_flags(argv: list[str]) -> tuple[float, float]:
    """Read `(stall_seconds, budget_seconds)` out of `argv` and REMOVE both.
    Either absent → 0.0, which disables that bound.

    Removal is not tidiness. `agent/loop.py:run()` parses whatever is left with a
    strict `parse_args()`, and it runs *after* the row is claimed — so an argument
    it does not recognise is a `SystemExit(2)` for every agent the box launches,
    at the one moment the row reads 'starting' under a pid that is on its way out.
    Declaring the flags on that parser instead is not an option: they have to be
    read before the import chain that builds the parser exists at all.

    So this module owns them end to end — reading them, acting on them, and taking
    them back off the command line before anyone else parses one.
    """
    return (_take(argv, STALL_FLAG), _take(argv, BUDGET_FLAG))


def _take(argv: list[str], flag: str) -> float:
    if flag not in argv:
        return 0.0
    at = argv.index(flag)
    raw = argv[at + 1] if at + 1 < len(argv) else ""
    del argv[at : at + 2]
    try:
        return float(raw)
    except ValueError:
        # A malformed window disables that bound rather than killing the boot:
        # the launcher's own confirm still bounds this launch, so refusing to
        # start would trade a weakened guarantee for no agent at all. The flag is
        # still stripped — leaving it behind would kill the boot anyway, for an
        # unrelated reason.
        return 0.0


def arm(agent_id: int, stall_seconds: float, budget_seconds: float) -> None:
    """Start watching this boot under two bounds, whichever fires first. Call
    before the import chain, after the first `_boot_timing.mark`.

    `stall_seconds` is the interesting one — no new phase within it means the boot
    has stopped moving. `budget_seconds` is the ceiling on the whole pre-claim
    boot, and it exists because the stall window alone does not actually bound
    anything: a boot that keeps reaching phases can live for phases x stall, a
    product that grows silently the day someone adds a mark. Past
    `allocated_reap_grace_seconds` the restarter's allocated-reaper takes the row
    out from under a child that is alive and progressing — the 2026-07-30 incident
    exactly, moved from the launcher to the reaper — so the budget is held below
    that grace and the reaper never meets a live child.

    Each bound is disabled by `<= 0` (`AVA_AGENT_BOOT_STALL_SECONDS` /
    `AVA_AGENT_BOOT_BUDGET_SECONDS`); both disabled arms nothing at all.

    The thread is a daemon: an agent that fails its schema gate raises out of the
    boot and the process exits normally, without this watchdog holding it open.
    """
    if stall_seconds <= 0 and budget_seconds <= 0:
        return
    _disarmed.clear()
    threading.Thread(
        target=_watch,
        args=(agent_id, stall_seconds, budget_seconds),
        name="ava-boot-deadline",
        daemon=True,
    ).start()


def disarm() -> None:
    """Stop watching — the row is claimed, so the row itself is now the signal.

    Past the CAS the agent is observable the ordinary way: the row carries its
    pid and status, and the restarter's `starting`/`running` reapers own it. A
    boot watchdog that outlived the claim would be a second, blinder authority
    over a row that already has one.
    """
    _disarmed.set()


def _watch(agent_id: int, stall_seconds: float, budget_seconds: float) -> None:
    """Poll the phase count; exit the process on whichever bound trips first.

    Patience follows progress: every new phase resets the stall clock, so a
    genuinely slow boot is not cut off for being slow, while a stalled one dies
    `stall_seconds` after its last real step however quick the boot had been
    until then. The budget clock never resets, and it is what makes the total
    bounded — "the phase count is finite" bounds the boot only at phases x stall,
    which is arithmetic over a number that changes whenever the boot path does.
    """
    started = time.monotonic()
    reached, _ = _boot_timing.progress()
    last_progress_at = started
    while not _disarmed.wait(_POLL_SECONDS):
        now = time.monotonic()
        now_reached, phase = _boot_timing.progress()
        if now_reached != reached:
            reached, last_progress_at = now_reached, now
        if budget_seconds > 0 and now - started >= budget_seconds:
            _die(agent_id, phase, budget_seconds, bound="total boot budget")
            return  # unreachable in production — `_die` exits the process
        if stall_seconds > 0 and now - last_progress_at >= stall_seconds:
            _die(agent_id, phase, stall_seconds, bound="no boot progress")
            return


def _die(agent_id: int, phase: str, seconds: float, *, bound: str) -> None:
    """Report which bound tripped and which phase the boot was in, then exit hard.

    Naming the bound is what separates the two failures for an operator: "no boot
    progress" means this box wedged, while "total boot budget" means the boot was
    moving the whole time and simply had nowhere left to go before the reaper
    would have taken the row — a signal to look at box load or the budget, not at
    a hang.

    `os._exit` rather than an exception or `sys.exit`: this runs on a watchdog
    thread, so raising would end only this thread, and the main thread may be
    blocked inside a C call where no Python-level interrupt is ever delivered —
    which is precisely the wedge being escaped. `_exit` skips atexit handlers and
    buffer flushing, hence the explicit `flush=True`: the line is the whole
    operator-facing artifact, and it lands in the per-agent stderr log the
    launcher's own failure message points at.

    The phase name is the part no external observer could ever produce. "Agent 41
    stopped during `starting_import`" says the import chain wedged; "during
    `schema_check`" says the data plane did.
    """
    print(  # noqa: T201 — early-boot stderr diagnostic; the logger is not imported yet, by design
        f"  [boot deadline] agent {agent_id} hit '{bound}' after {seconds:.0f}s, "
        f"last phase reached '{phase}'. Exiting so the launcher sees a dead process "
        f"instead of waiting out its window.",
        file=sys.stderr,
        flush=True,
    )
    os._exit(1)
