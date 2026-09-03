"""The child's boot watchdog — `agent/_boot_deadline.py`.

What is under test is one guarantee the rest of the launch machinery leans on:
**while an agent process is alive, its pre-claim boot is progressing.** The
launcher cannot check that (the row reads unclaimed 'idling' with no pid the whole
time), so the child enforces it by exiting when it stops being true, and
`ops/agent_launch.py` reads "the process exists" as "the boot is moving".

Every test drives `_watch` directly on a virtual clock rather than through
`arm`'s thread: the loop's own `time.monotonic` is the thing being asserted
about, and a real-clock version of "did it wait long enough" is exactly the
flaky-on-a-loaded-runner test this whole feature exists to stop writing.
"""

from __future__ import annotations

import os
import threading

import pytest

from agent import _boot_deadline, _boot_timing


class _FakeClock:
    """Virtual time driven by the watchdog's own poll wait.

    `monotonic()` reads the dial; the stand-in for `Event.wait` advances it by
    one poll and reports "not disarmed", so the loop walks forward in exact,
    reproducible steps and the test finishes instantly.

    `max_waits` is a tripwire, not a parameter of any test: a watchdog that fails
    to bound a boot does not fail an assertion, it spins here forever, and a
    hanging suite is a much worse signal than a red one. Blowing up at a wait
    count no passing test comes near turns "never terminated" into a normal
    failure with a message that says so.
    """

    _MAX_WAITS = 10_000

    def __init__(self, poll: float, *, disarmed_after: int | None = None) -> None:
        self.now = 0.0
        self._poll = poll
        self._waits = 0
        self._disarmed_after = disarmed_after

    def monotonic(self) -> float:
        return self.now

    def wait(self, _timeout: float) -> bool:
        self.now += self._poll
        self._waits += 1
        if self._waits > self._MAX_WAITS:
            raise AssertionError(
                f"the watchdog polled {self._waits} times ({self.now:.0f} virtual seconds) "
                "without ever exiting or being disarmed — this boot is unbounded"
            )
        return self._disarmed_after is not None and self._waits > self._disarmed_after


@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Capture `os._exit` instead of ending the test session with it."""
    codes: list[int] = []
    monkeypatch.setattr(os, "_exit", codes.append)
    return codes


def _install(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(_boot_deadline, "time", clock)
    monkeypatch.setattr(_boot_deadline._disarmed, "wait", clock.wait)


def _frozen_progress(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """A boot that has reached `phase` and never reaches another."""
    monkeypatch.setattr(_boot_timing, "progress", lambda: (1, phase))


def test_a_boot_that_stops_progressing_exits_the_process(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    """The core behavior: no new phase within the window and the child removes
    itself, rather than holding a pid the launcher will misread as a live boot."""
    clock = _FakeClock(poll=0.5)
    _install(monkeypatch, clock)
    _frozen_progress(monkeypatch, "starting_import")

    _boot_deadline._watch(41, stall_seconds=10.0, budget_seconds=0.0)

    assert exits == [1]
    assert clock.now >= 10.0


def test_the_diagnostic_names_the_phase_the_boot_died_in(
    monkeypatch: pytest.MonkeyPatch, exits: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    """The phase name is the part no external observer could ever produce, and it
    is the whole operator-facing artifact — `os._exit` skips normal teardown, so
    if this line is missing or unflushed the failure is anonymous."""
    clock = _FakeClock(poll=0.5)
    _install(monkeypatch, clock)
    _frozen_progress(monkeypatch, "schema_check")

    _boot_deadline._watch(41, stall_seconds=10.0, budget_seconds=0.0)

    err = capsys.readouterr().err
    assert "agent 41" in err
    assert "schema_check" in err


def test_patience_follows_progress_not_total_elapsed(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    """The property that separates this from a bigger timeout.

    A boot that keeps reaching phases is never cut off, however long it has been
    running in total — here it runs 6x the stall window and lives — and the same
    boot dies one stall window after its LAST phase. That is why the number
    bounds a single phase rather than the whole boot, and so does not have to be
    re-guessed every time the import chain grows or the box gets busier.
    """
    reached = {"n": 1}
    phases_to_reach = 12

    def _creeping_progress() -> tuple[int, str]:
        if reached["n"] <= phases_to_reach:
            reached["n"] += 1
        return (reached["n"], f"phase-{reached['n']}")

    monkeypatch.setattr(_boot_timing, "progress", _creeping_progress)
    clock = _FakeClock(poll=0.5)
    _install(monkeypatch, clock)

    _boot_deadline._watch(41, stall_seconds=1.0, budget_seconds=0.0)

    assert reached["n"] > phases_to_reach, (
        "the watchdog killed a boot that was still reaching new phases — patience "
        "is following the wall clock, not progress"
    )
    assert clock.now > 6.0, "the surviving stretch must exceed the stall window many times over"
    assert exits == [1], "once progress stopped, the stall window still applied"


def test_the_budget_stops_a_boot_that_progresses_forever(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    """The bound the stall window cannot supply.

        Progress resets the stall clock, so a boot that keeps reaching phases is
        unbounded by it — "the phase count is finite" only bounds the boot at
        phases x stall, arithmetic over a number that moves whenever someone adds a
    mark. Meanwhile the restarter's dead-birth reaper takes an unclaimed 'idling' row on
        age alone (its clock is `status_changed_at`, which only a status flip resets),
        so a boot that outran that grace would have its row reaped out from under a
        live, progressing child. The budget clock never resets, which is the point.
    """
    ticks = {"n": 0}

    def _endless_progress() -> tuple[int, str]:
        ticks["n"] += 1
        return (ticks["n"], f"phase-{ticks['n']}")

    monkeypatch.setattr(_boot_timing, "progress", _endless_progress)
    clock = _FakeClock(poll=0.5)
    _install(monkeypatch, clock)

    _boot_deadline._watch(41, stall_seconds=10.0, budget_seconds=20.0)

    assert exits == [1], "a forever-progressing boot must still be stopped"
    assert clock.now >= 20.0
    assert clock.now < 30.0, "the budget, not the stall window, is what ended it"


def test_the_budget_names_itself_so_a_slow_boot_is_not_read_as_a_hang(
    monkeypatch: pytest.MonkeyPatch, exits: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    """Two very different operator stories share one exit. "no boot progress"
    means this box wedged; "total boot budget" means the boot was moving the whole
    time and simply ran out of room before the reaper would have taken the row —
    look at load or at the budget, not for a hang."""
    monkeypatch.setattr(_boot_timing, "progress", lambda: (1, "starting_import"))
    clock = _FakeClock(poll=0.5)
    _install(monkeypatch, clock)

    _boot_deadline._watch(41, stall_seconds=0.0, budget_seconds=20.0)

    assert "total boot budget" in capsys.readouterr().err


def test_a_disarmed_watchdog_stops_watching(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    """Past the CAS the row carries a pid and the restarter's reapers own it. A
    boot watchdog still running then would be a second, blinder authority over an
    agent that is legitimately busy doing its heavy imports."""
    clock = _FakeClock(poll=0.5, disarmed_after=2)
    _install(monkeypatch, clock)
    _frozen_progress(monkeypatch, "enter_starting")

    _boot_deadline._watch(41, stall_seconds=10.0, budget_seconds=0.0)

    assert exits == [], "a disarmed watchdog must not kill the process it stopped watching"


def test_zero_stall_seconds_arms_nothing() -> None:
    """The operator kill switch (`AVA_AGENT_BOOT_STALL_SECONDS=0`). It really has
    to start no thread: a watchdog armed with a nonsensical window would fire on
    its first poll and kill every agent the box launches."""
    _boot_deadline.arm(41, 0.0, 0.0)

    assert not [t for t in threading.enumerate() if t.name == "ava-boot-deadline"]


def test_arm_starts_a_daemon_thread_that_disarm_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real threading path, since every other test bypasses it. Daemon is
    load-bearing: a boot rejected by the schema gate raises out and the process
    exits normally, which a non-daemon watchdog would hold open.

    The stall window is set absurdly high on purpose. This is the one test that
    arms a thread holding the REAL `os._exit`, so if the disarm below ever failed
    to stop it, a reachable window would let it kill the pytest worker minutes
    later, somewhere unrelated. A window no test session outlives makes that
    failure show up as this test's own assertion instead."""
    monkeypatch.setattr(_boot_deadline, "_POLL_SECONDS", 0.01)

    _boot_deadline.arm(41, 10_000.0, 10_000.0)
    armed = [t for t in threading.enumerate() if t.name == "ava-boot-deadline"]
    assert len(armed) == 1
    assert armed[0].daemon

    _boot_deadline.disarm()
    armed[0].join(timeout=5.0)
    assert not armed[0].is_alive()


def test_consume_flags_reads_both_windows_and_removes_them() -> None:
    """Both halves matter, and the removal is the one with teeth.

    `agent/loop.py:run()` parses the leftover argv with a strict `parse_args()`,
    after the row is already claimed — so a flag left behind is a `SystemExit(2)`
    for every agent on the box at the worst possible moment. Declaring them on
    that parser instead is not available: they must be read before the import
    chain that builds the parser exists."""
    argv = [
        "python -m agent",
        "--agent-id",
        "7",
        "--boot-stall-seconds",
        "30.0",
        "--boot-budget-seconds",
        "90.0",
    ]

    assert _boot_deadline.consume_flags(argv) == (30.0, 90.0)
    assert argv == ["python -m agent", "--agent-id", "7"]


def test_consume_flags_without_the_flags_is_a_no_op() -> None:
    """An agent started by hand carries no windows and arms nothing; the argv it
    did pass must survive untouched for `run()` to parse."""
    argv = ["python -m agent", "--agent-id", "7"]

    assert _boot_deadline.consume_flags(argv) == (0.0, 0.0)
    assert argv == ["python -m agent", "--agent-id", "7"]


def test_a_malformed_window_disables_that_bound_instead_of_the_agent() -> None:
    """Degrade to the pre-existing guarantee, not to no agent at all: the
    launcher's own confirm still bounds this launch, so refusing to boot would
    trade a weakened watchdog for a dead agent. Only the malformed bound is lost —
    the other still applies — and both flags are stripped regardless, since
    leaving one behind would kill the boot for an unrelated reason entirely."""
    argv = [
        "python -m agent",
        "--agent-id",
        "7",
        "--boot-stall-seconds",
        "not-a-number",
        "--boot-budget-seconds",
        "90.0",
    ]

    assert _boot_deadline.consume_flags(argv) == (0.0, 90.0)
    assert argv == ["python -m agent", "--agent-id", "7"]


@pytest.mark.parametrize(
    ("restart_trace", "expects_marker"),
    [
        (("self", "", None), True),
        (None, False),
    ],
    ids=["restart-trace", "missing-restart-trace"],
)
def test_every_agent_launch_path_arms_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    restart_trace: tuple[str, str, None] | None,
    expects_marker: bool,
) -> None:
    """Coverage is per *launcher*: `agent/db.py:schedule_self_respawn` builds its
    own argv rather than going through `ops/agent_launch.py`, so it needs its own
    pin here instead of inheriting one.

    A launch that omits the flag arms nothing and fails silently — the child boots
    without a watchdog and `_launched_process_alive` quietly goes back to meaning
    "a pid exists". This path is the worst place for that to happen: it only runs
    when the restarter is paused, i.e. mid-rollout, when boxes are at their
    busiest and a stalled boot is most likely.

    The fallback must also preserve a restart marker when it has a trace, but
    still launch if a self-initiated restart's trace is unexpectedly absent.
    """
    import atexit
    import subprocess
    import time
    from typing import Any

    import psycopg

    from agent.db import schedule_self_respawn

    executed: list[tuple[str, tuple[object, ...]]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self) -> None:
            self._row: tuple[object, ...] | None = None

        def execute(self, query: str, *_a: Any, **_kw: Any) -> None:
            executed.append((query, _a[0] if _a else ()))
            if "SELECT status" in query:
                self._row = ("restarting",)
            elif "SELECT source, content, payload" in query:
                self._row = restart_trace
            elif "SELECT config_overlay" in query:
                self._row = (None, None)

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def transaction(self) -> _FakeConn:
            return self

        def execute(self, *_a: Any, **_kw: Any) -> None:
            return None

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def close(self) -> None:
            pass

    registered: list[Any] = []
    launched: list[list[str]] = []
    clock = {"now": 0.0}

    def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(atexit, "register", registered.append)
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", _sleep)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_kw: _FakeConn())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **_kw: launched.append([str(a) for a in argv]),  # pyright: ignore[reportUnknownArgumentType]
    )

    schedule_self_respawn(11)
    registered[0]()  # the atexit callable

    assert launched, "the self-respawn should have launched a replacement"
    argv = launched[0]
    restart_completed_inserts = [
        params
        for query, params in executed
        if "INSERT INTO inbound_messages" in query and "restart_completed" in query
    ]
    assert restart_completed_inserts == ([(11, "", "self", None)] if expects_marker else [])

    from shared.config import settings

    for flag, expected in (
        ("--boot-stall-seconds", settings.gateway.agent_boot_stall_seconds),
        ("--boot-budget-seconds", settings.gateway.agent_boot_budget_seconds),
    ):
        assert flag in argv, f"a self-respawned agent boots without {flag}, and nothing reports it"
        assert float(argv[argv.index(flag) + 1]) == expected


def test_self_respawn_gives_the_restarter_priority_until_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarter claim seen during the bounded fallback window prevents a second child."""
    import atexit
    import subprocess
    from typing import Any

    import psycopg

    from agent import db as agent_db

    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self._row: tuple[object, ...] | None = None
            self._status_reads = 0

        def execute(self, query: str, *_args: Any, **_kwargs: Any) -> None:
            if "SELECT status" in query:
                self._status_reads += 1
                self._row = ("restarting" if self._status_reads == 1 else "idling",)
            elif "SELECT source, content, payload" in query:
                self._row = ("self", "", None)
            elif "SELECT config_overlay" in query:
                self._row = (None, None)

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _Connection:
        def __init__(self) -> None:
            self.cursor_value = _Cursor()

        def cursor(self) -> _Cursor:
            return self.cursor_value

        def close(self) -> None:
            return None

    registered: list[Any] = []
    launched: list[list[str]] = []
    clock = iter((0.0, 0.0, 0.1, 0.1))

    def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(atexit, "register", registered.append)
    monkeypatch.setattr(agent_db.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(agent_db.time, "sleep", _sleep)
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: _Connection())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **_kwargs: launched.append([str(arg) for arg in argv]),  # pyright: ignore[reportUnknownArgumentType]
    )

    agent_db.schedule_self_respawn(11)
    registered[0]()

    assert launched == []


def test_progress_reports_the_count_and_the_last_phase() -> None:
    """`_boot_timing.progress` is the watchdog's only input: the count is the
    progress signal, the name is what the child reports on the way out."""
    _boot_timing._marks.clear()
    assert _boot_timing.progress() == (0, "")

    _boot_timing.mark("start")
    _boot_timing.mark("starting_import")
    try:
        assert _boot_timing.progress() == (2, "starting_import")
    finally:
        _boot_timing._marks.clear()
