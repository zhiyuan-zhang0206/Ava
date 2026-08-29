"""Session entrypoint for a gateway-hosted schedule: ``python -m gateway.schedule_runner <id>``.

Loads schedule ``<id>`` from the DB, materializes its script under
``$AVA_HOME/schedules/<id>/``, binds a ``schedule:<id>`` actor identity (so the
script's ``ava.agents.*`` calls attribute to the schedule instead of failing for
lack of an agent id), then runs it. A ``.py`` script runs in-process via runpy so
it shares the bound actor; any other command runs as a subprocess. An uncaught
crash's traceback is written to ``schedules.last_error``.

Version-controlled schedule templates (manifest + scripts) live in
``schedules/`` — provisioned via ``shared.builtin_schedules.py``.

Every process execution appends one row to ``schedule_runs`` (the run-history
drawer's data source): opened with ``ok = NULL`` (in-progress) when the runner
starts, closed with the outcome when it exits. This is process-level history —
one row per process lifetime, not per fire — and it is severable observability:
a run-record write failure never affects the schedule itself. A run the runner
cannot close (a SIGTERM/SIGHUP kill, a manager SIGKILL on stop/restart/
edited-script save) stays ``ok = NULL`` until the ScheduleManager's reconcile
sweep closes it as ``interrupted`` — a NULL row is legitimate only while the
schedule has a live session; a stall-guard hard exit closes it ``ok = false``
before dying.

The ScheduleManager launches this inside a session named
``ava-schedule-<id>`` and keeps it up (with a circuit breaker) if it
crashes. A schedule is a supervised resident process, so an exit-0 is treated as
a deliberate finish: the runner records ``status='completed'`` and the manager
leaves it alone (no relaunch, no breaker). A nonzero exit / uncaught exception is
a crash — its traceback goes to ``schedules.last_error`` and the manager restarts
it. A deliberate kill (SIGTERM/SIGHUP) writes nothing and is not counted a crash.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
import traceback
from contextlib import suppress
from pathlib import Path

from loguru import logger

import shared.db
from shared.config import settings
from shared.paths import ava_home, prod_service_checkout_error

# A .py schedule script is run in-process, so a single call that hangs (a
# wedged gateway, a black-holed DB connection, a stuck import) parks the whole
# runner with no crash and no last_error — 2026-08-03: the self-evolution
# weekly schedule sat "alive" through the gateway's 08:28-09:23 freeze window
# and silently missed its Monday 9am fire. The stall guard watches the main
# thread's stack and hard-exits after a frame has not advanced for this long,
# so the ScheduleManager's crash path (backoff + breaker + last_error) gets a
# chance instead of a zombie.
_STALL_TIMEOUT_S = settings.gateway.schedule_stall_timeout_seconds
_STALL_CHECK_INTERVAL_S = settings.gateway.schedule_stall_check_interval_seconds

# Frames that legitimately park the main thread for unbounded time — a
# resident schedule's whole reason for existing is a long sleep between fire
# windows. The DEEPEST frame decides: a sleep on top of the stack means the
# script is deliberately parked, not stalled.
_PARK_FRAME_NAMES = frozenset({"sleep", "wait", "wait_for", "run_forever", "acquire"})


def _schedule_dir(schedule_id: int) -> Path:
    return ava_home() / "schedules" / str(schedule_id)


# Filename suffixes that mean "this token IS the script" — a bare token with
# any other dotted name (a versioned interpreter like ``python3.11``, an
# extensionless binary path, a dotted flag value) must not be mistaken for
# the script file the runner materializes (audit gateway.md P2-8).
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".bash", ".zsh"})


def _script_filename(command: str) -> str:
    """The file the script is written to — the first token in ``command``
    whose name ends in a known script extension, else ``schedule.py``. So
    ``python schedule.py`` -> ``schedule.py``; ``bash run.sh`` -> ``run.sh``;
    ``python3.11 main.py`` -> ``main.py`` (not ``python3.11``)."""
    for token in shlex.split(command):
        name = Path(token).name
        if name.endswith(tuple(_SCRIPT_SUFFIXES)) and not token.startswith("-"):
            return name
    return "schedule.py"


def _load(schedule_id: int) -> tuple[str, str] | None:
    """Return (script, command) for an enabled schedule, or None if it is gone /
    disabled (a benign race: the manager launched it, then it was deleted)."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT script, command FROM schedules WHERE id = %s AND enabled = true",
            (schedule_id,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row is not None else None


def _record_error(schedule_id: int, message: str) -> None:
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET last_error = %s, updated_at = now() WHERE id = %s",
            (message, schedule_id),
        )


def _mark_completed(schedule_id: int) -> None:
    """Record a clean exit (rc=0) as the terminal `completed` status. A schedule
    is a supervised resident process, so an exit-0 is a deliberate finish, not a
    crash — this is the durable signal the ScheduleManager reads to leave the
    schedule alone instead of relaunching / counting it toward the crash breaker.
    The manager reads liveness before status, so a session that is gone is
    guaranteed to have this write already committed (see schedule_manager)."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET status = 'completed', updated_at = now() WHERE id = %s",
            (schedule_id,),
        )


def _finish_completed(schedule_id: int, run_id: int | None) -> None:
    """Record a clean finish: mark the schedule completed and close the run
    row ok=true. The completed-marker write is best-effort — if it fails, the
    manager's liveness-before-status rule makes it relaunch the schedule (the
    safe side), but the run row must still read as a success: the run itself
    finished, only the bookkeeping lost a write (QA P3-5 — it must not be
    recorded as 'crashed: OperationalError')."""
    try:
        _mark_completed(schedule_id)
    except Exception:
        logger.exception("schedule {} completed-marker write failed", schedule_id)
        _record_run_end(run_id, ok=True, note="completed-marker write failed")
    else:
        _record_run_end(run_id, ok=True, note=None)


def _record_run_start(schedule_id: int) -> int | None:
    """Open a run-history row for this process execution (ok = NULL, in-progress).

    Returns the run id, or None when the write fails — run history is severable
    observability, so a DB hiccup must never break the schedule itself (the
    caller then skips the closing write)."""
    try:
        with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule_runs (schedule_id) VALUES (%s) RETURNING id",
                (schedule_id,),
            )
            row = cur.fetchone()
            return row[0] if row is not None else None
    except Exception:
        logger.exception("schedule {} run-record start failed", schedule_id)
        return None


def _record_run_end(run_id: int | None, *, ok: bool, note: str | None) -> None:
    """Close a run-history row with its outcome. No-op when the start write
    failed (run_id is None); a failure here is likewise never fatal."""
    if run_id is None:
        return
    try:
        with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = %s, note = %s WHERE id = %s",
                (ok, note, run_id),
            )
    except Exception:
        logger.exception("schedule run-record end failed (run {})", run_id)


_ORIGINAL_SLEEP = time.sleep


def _patch_park_detection() -> None:
    """Wrap ``time.sleep`` so the stall guard can see a deliberate park.

    ``time.sleep`` is a C builtin: the deepest *Python* frame of a main thread
    blocked inside it is the caller of sleep — a stable frame, exactly like a
    stall. The wrapper is named ``sleep`` (a ``_PARK_FRAME_NAMES`` member), so
    a main thread parked in it reads as parked, while a thread stuck in any
    other call still reads as stalled. Semantics are unchanged — the wrapper
    just forwards to the original."""

    def sleep(delay: float) -> None:
        _ORIGINAL_SLEEP(delay)

    time.sleep = sleep


def _restore_park_detection() -> None:
    """Undo ``_patch_park_detection``: put the stdlib ``time.sleep`` back.

    The wrapper exists only for the stall guard's judgment window; leaving it
    installed past the guard's stop swaps `time.sleep` for a Python function
    process-wide in this runner (the runner is its own process — one schedule
    per `python -m gateway.schedule_runner <id>` session — but a
    ``.py`` schedule script runs in-process here, so the swap would leak into
    the rest of its run), which a later ``assert time.sleep is _REAL_SLEEP``
    guard in the test suite trips on. Idempotent and safe to call without a
    patch."""
    time.sleep = _ORIGINAL_SLEEP


def _stall_action(schedule_id: int, message: str, run_id: int | None) -> None:
    """The stall verdict: record ``last_error``, close the run-history row as
    failed (a hard-exit is an abnormal end, like a crash), then hard-exit so
    the ScheduleManager's crash path (backoff + breaker) relaunches the
    schedule."""
    with suppress(Exception):
        _record_error(schedule_id, message)
    logger.error("Schedule {} {}", schedule_id, message)
    _record_run_end(run_id, ok=False, note=f"stalled ({_STALL_TIMEOUT_S:.0f}s)")
    os._exit(1)  # hard exit — the schedule manager owns the restart


def _start_stall_guard(schedule_id: int, run_id: int | None) -> threading.Event:
    """Watch the main thread for a stall and hard-exit when one is found.

    Returns a stop event; the caller sets it once the script returns so the
    guard cannot kill the process between a clean return and the completed
    marker write.

    Every ``_STALL_CHECK_INTERVAL_S`` the guard captures the main thread's
    deepest frame. A frame that has not changed for ``_STALL_TIMEOUT_S`` is a
    stall (a single call — HTTP, DB, import — that never returned): the guard
    records ``last_error`` and ``os._exit(1)`` so the ScheduleManager's crash
    path (backoff + breaker) relaunches the schedule instead of leaving a
    zombie that never fires. The deepest frame being a park frame
    (``time.sleep`` / ``Event.wait`` / ...) is the legitimate idle of a
    resident schedule and is ignored.
    """
    main_thread_id = threading.get_ident()
    stop = threading.Event()

    def _guard() -> None:
        last_sig: tuple[str, int, str] | None = None
        stalled_since: float | None = None
        while not stop.is_set():
            time.sleep(_STALL_CHECK_INTERVAL_S)
            try:
                frame = sys._current_frames().get(main_thread_id)
                if frame is None:
                    continue
                if frame.f_code.co_name in _PARK_FRAME_NAMES:
                    last_sig = None
                    stalled_since = None
                    continue
                sig = (frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)
            except Exception as exc:
                # The guard must never crash the runner; a failed frame read is
                # just one skipped check.
                logger.debug("schedule stall guard frame read failed: {}", exc)
                continue
            now = time.monotonic()
            if sig == last_sig and stalled_since is not None:
                if now - stalled_since >= _STALL_TIMEOUT_S:
                    message = (
                        f"schedule runner stalled {now - stalled_since:.0f}s in "
                        f"{sig[2]} ({sig[0]}:{sig[1]}) — hard-exiting; check the "
                        "gateway / DB / network the script calls into"
                    )
                    _stall_action(schedule_id, message, run_id)
            elif sig != last_sig:
                last_sig = sig
                stalled_since = now

    threading.Thread(target=_guard, name=f"schedule-{schedule_id}-stall-guard", daemon=True).start()
    return stop


def _record_script_exit(schedule_id: int, run_id: int | None, exc: SystemExit) -> int:
    """Record a .py script's deliberate sys.exit() like a command's exit code
    (QA P3-3 — it must not leave the run row in-progress forever). A None code
    is 0; a non-int code (a message string) is 1. int() normalizes bools (the
    interpreter treats them as exit codes: True -> 1, False -> 0); a non-int
    code is a message the interpreter would print to stderr, so it rides the
    note instead of being swallowed (QA N1)."""
    code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code == 0:
        _finish_completed(schedule_id, run_id)
    else:
        message = "" if isinstance(exc.code, int) else f": {exc.code}"
        _record_error(schedule_id, f"script exited {code}{message}")
        _record_run_end(run_id, ok=False, note=f"script exited {code}{message}")
    return code


def run(schedule_id: int) -> int:
    """Materialize + run the schedule. Returns a process exit code."""
    loaded = _load(schedule_id)
    if loaded is None:
        logger.warning("Schedule {} is gone or disabled; nothing to run", schedule_id)
        return 0
    script, command = loaded

    work_dir = _schedule_dir(schedule_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    script_name = _script_filename(command)
    script_path = work_dir / script_name
    script_path.write_text(script)

    # Bind the actor so ava.agents.* attributes this schedule's spawns/wakes to
    # `schedule:<id>` (a .py script, run in-process below, shares this binding).
    import ava._boot

    ava._boot.establish_actor(f"schedule:{schedule_id}")

    # Run history: one row per process execution, opened in-progress (ok=NULL)
    # here and closed with the outcome on every exit path below — including the
    # stall guard, which receives run_id and closes the row ok=false before its
    # hard exit. Only a kill that leaves no code path to close (SIGTERM/SIGHUP/
    # SIGKILL) leaves the row in-progress; the manager's reconcile sweep closes
    # it as 'interrupted' once the process is gone.
    run_id = _record_run_start(schedule_id)

    try:
        if script_name.endswith(".py"):
            import runpy

            import ava

            # Load plugin namespaces (ava.tasks etc.) into this process before the
            # in-process script runs. This runner never builds the agent graph, so
            # nothing else loads plugins here; without this, a schedule script that
            # touches ava.tasks would hit the factory `import ava` and AttributeError.
            # Only the .py-in-process branch needs it — the other branch runs a
            # non-.py shell command (bash etc.) that does not import ava.
            # Stall guard: a hung call inside the script (or in plugin
            # loading below) must not leave the runner alive-but-silent
            # (2026-08-03 self-evolution miss). Started before plugin loading
            # so an import hang is covered too. Stopped before _mark_completed
            # so a clean return cannot be overtaken by a spurious kill.
            _patch_park_detection()
            stop_guard = _start_stall_guard(schedule_id, run_id)
            try:
                ava._ensure_plugins_loaded()
                runpy.run_path(str(script_path), run_name="__main__")
            finally:
                stop_guard.set()
                # The park wrapper is process-wide; the guard's judgment window
                # is over, so restore the stdlib sleep — it must not leak into
                # the rest of this process.
                _restore_park_detection()
            _finish_completed(schedule_id, run_id)  # clean return => finished, not crashed
            return 0
        # A non-.py command runs as a child process — the stall guard's main-
        # thread frame watch cannot see inside it, and the runner parked in
        # subprocess.run would read as a legitimate park anyway ("wait" is a
        # park frame). Bound it with the same stall timeout instead: a command
        # that has not finished within the budget is hung, not long-running —
        # without a bound, a never-exiting command would sit forever with no
        # last_error and no breaker fire, silently eating every future fire
        # window (2026-08-08 audit, P2-2 — the .py branch got its stall guard
        # after the 2026-08-03 self-evolution miss; the command branch was
        # still open). subprocess.run kills the child on expiry and raises
        # TimeoutExpired; the crash path (backoff + breaker) relaunches.
        try:
            result = subprocess.run(  # noqa: S603 — command is the operator-authored schedule command
                shlex.split(command),
                cwd=str(work_dir),
                check=False,
                timeout=_STALL_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            message = (
                f"command did not finish within {_STALL_TIMEOUT_S:.0f}s "
                f"(stall timeout): {command!r}"
            )
            _record_error(schedule_id, message)
            logger.error("Schedule {} {}", schedule_id, message)
            _record_run_end(run_id, ok=False, note=f"stall timeout ({_STALL_TIMEOUT_S:.0f}s)")
            return 1
        if result.returncode != 0:
            _record_error(schedule_id, f"command exited {result.returncode}: {command!r}")
            _record_run_end(run_id, ok=False, note=f"command exited {result.returncode}")
        else:
            _finish_completed(schedule_id, run_id)
        return result.returncode
    except SystemExit as exc:
        return _record_script_exit(schedule_id, run_id, exc)
    except Exception as exc:
        tb = traceback.format_exc()
        _record_error(schedule_id, tb)
        logger.error("Schedule runner execution failed: {}", tb)
        _record_run_end(run_id, ok=False, note=f"crashed: {type(exc).__name__}")
        return 1


def main() -> None:
    if len(sys.argv) != 2:
        logger.error("Usage: python -m gateway.schedule_runner <schedule_id>")
        raise SystemExit(2)
    # issue #194: refuse to run from a foreign checkout (a dev worktree
    # against the prod home) — the runner's own repo root anchors every
    # subprocess it spawns, so a worktree-anchored runner executes un-reviewed
    # code and dies silently when the worktree is removed.
    refusal = prod_service_checkout_error(Path(__file__).resolve().parents[1])
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    raise SystemExit(run(int(sys.argv[1])))


if __name__ == "__main__":
    main()
