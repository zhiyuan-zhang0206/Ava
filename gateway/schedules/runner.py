"""Schedule execution engine, composed by ``services.wake.schedule_manager.runner``.

Loads schedule ``<id>`` from the DB, materializes its script under
``$AVA_HOME/schedules/<id>/``, binds a ``schedule:<id>`` actor identity (so the
script's ``ava.agents.*`` calls attribute to the schedule instead of failing for
lack of an agent id), then runs it. A ``.py`` script runs in-process via runpy so
it shares the bound actor; any other command runs as a subprocess. An uncaught
crash's traceback is written to ``schedules.last_error`` (the latest crash) and, truncated
to its last ``_NOTE_TRACEBACK_MAX`` characters, to the run's ``schedule_runs.note`` (one
per crash).

Version-controlled schedule templates (manifest + scripts) live in
``schedules/`` — provisioned via ``base/daemon/schedules/builtin_schedules.py``.

Every process execution appends one row to ``schedule_runs`` (the run-history
drawer's data source): opened with ``ok = NULL`` (in-progress) when the runner
starts, closed with the outcome when it exits. This is process-level history —
one row per process lifetime, not per fire — and it is severable observability:
a run-record write failure never affects the schedule itself. A run the runner
cannot close (a SIGTERM/SIGHUP kill, a manager SIGKILL on stop/restart/
edited-script save) stays ``ok = NULL`` until the ScheduleManager's reconcile
sweep closes it as ``interrupted`` — a NULL row is legitimate only while the
schedule has a live session. A stall-guard hard exit reaps owned descendants,
then attempts to close it ``ok = false`` within a bounded record deadline.

The ScheduleManager launches this inside a session named
``ava-schedule-<id>`` with one admitted launch per desired revision. A schedule is a supervised resident process, so an exit-0 is treated as
a deliberate finish: the runner records ``status='completed'`` and the manager
leaves it alone. A nonzero exit / uncaught exception records its traceback in
``schedules.last_error``. Process loss becomes terminal error; only an explicit
new revision authorizes another launch. A deliberate kill (SIGTERM/SIGHUP) writes nothing and is not counted a crash.
"""

from __future__ import annotations

import faulthandler
import os
import runpy
import selectors
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from loguru import logger

import base.host.proc
from base.config import settings
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.paths import ava_home


# A .py schedule script is run in-process, so a single call that hangs (a
# wedged gateway, a black-holed DB connection, a stuck import) parks the whole
# runner with no crash and no last_error — 2026-08-03: the self-evolution
# weekly schedule sat "alive" through the gateway's 08:28-09:23 freeze window
# and silently missed its Monday 9am fire. The stall guard watches the main
# thread's stack and hard-exits after a frame has not advanced for this long,
# so the schedule is recorded as failed (last_error) instead of a zombie.
def _stall_timeout_s() -> float:
    return settings.gateway.schedule_stall_timeout_seconds


# The guard samples the main thread this often (a quarter of the timeout when
# that is shorter, which only tests configure).
_SAMPLE_INTERVAL_MAX_S = 30.0

# Both failure writes together get this long after child cleanup; a DB that is
# the reason for the stall cannot keep the runner alive past it.
_FAILURE_RECORD_DEADLINE_S = 10.0

# What ``close`` waits for an admitted action: child cleanup (3s TERM grace +
# 5s reap), the record deadline, and 1s margin.
_HARD_EXIT_WAIT_S = 9.0 + _FAILURE_RECORD_DEADLINE_S


def _stall_check_interval_s() -> float:
    return min(_SAMPLE_INTERVAL_MAX_S, _stall_timeout_s() / 4)


# Frames that legitimately park the main thread for unbounded time — a
# resident schedule's whole reason for existing is a long sleep between fire
# windows. The DEEPEST frame decides: a sleep on top of the stack means the
# script is deliberately parked, not stalled. Child waits also park in
# subprocess's blocking waitpid or the selector wait immediately inside
# _communicate. Spawn, argument conversion, and stdin.flush stay guarded.
_PARK_FRAME_NAMES = frozenset({"sleep", "wait", "wait_for", "run_forever", "acquire"})

# Cache the code's filenames, the same identities the sampled frames carry.
# Unlike module __file__, these also work with loaders that omit __file__;
# a missing attribute must not turn every guard tick into a skipped check.
_SUBPROCESS_FILENAME = subprocess.Popen[bytes].wait.__code__.co_filename
_SELECTORS_FILENAME = selectors.SelectSelector.select.__code__.co_filename


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


def _load(
    database: Database, schedule_id: int, revision: int | None = None
) -> tuple[str, str] | None:
    """Return (script, command) for an enabled schedule, or None if it is gone /
    disabled (a benign race: the manager launched it, then it was deleted)."""
    if revision is None:
        with database.connect(autocommit=True) as conn:
            row = conn.execute(
                "SELECT script, command FROM schedules WHERE id = %s AND enabled = true",
                (schedule_id,),
            ).fetchone()
    else:
        # Admission fences a delayed runner from executing a newer script under
        # an old command identity. Mark applied before any user code so a quick
        # completion survives the manager's post-launch acknowledgement crash.
        with database.write_transaction() as conn:
            row = conn.execute(
                "UPDATE schedules SET applied_revision = %s, status = 'running', last_error = NULL "
                "WHERE id = %s AND enabled AND desired_revision = %s RETURNING script, command",
                (revision, schedule_id, revision),
            ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _record_error(database: Database, schedule_id: int, message: str) -> None:
    with database.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET last_error = %s, updated_at = now() WHERE id = %s",
            (message, schedule_id),
        )


def _mark_completed(database: Database, schedule_id: int) -> None:
    """Record a clean exit (rc=0) as the terminal `completed` status. A schedule
    is a supervised resident process, so an exit-0 is a deliberate finish, not a
    crash — this is the durable signal the ScheduleManager reads to leave the
    schedule completed rather than recording process loss as an error.
    The manager reads liveness before status, so a session that is gone is
    guaranteed to have this write already committed (see services/wake/schedule_manager/manager.py)."""
    with database.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET status = 'completed', updated_at = now() WHERE id = %s",
            (schedule_id,),
        )


def _finish_completed(database: Database, schedule_id: int, run_id: int | None) -> None:
    """Record a clean finish: mark the schedule completed and close the run
    row ok=true. The completed-marker write is best-effort — if it fails, the
    manager may record process loss as an error, but does not relaunch the
    schedule. The run row must still read as a success: the run itself
    finished, only the bookkeeping lost a write (QA P3-5 — it must not be
    recorded as 'crashed: OperationalError')."""
    try:
        _mark_completed(database, schedule_id)
    except Exception:
        logger.exception("schedule {} completed-marker write failed", schedule_id)
        _record_run_end(database, run_id, ok=True, note="completed-marker write failed")
    else:
        _record_run_end(database, run_id, ok=True, note=None)


def _record_run_start(database: Database, schedule_id: int) -> int | None:
    """Open a run-history row for this process execution (ok = NULL, in-progress).

    Returns the run id, or None when the write fails — run history is severable
    observability, so a DB hiccup must never break the schedule itself (the
    caller then skips the closing write)."""
    try:
        with database.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule_runs (schedule_id) VALUES (%s) RETURNING id",
                (schedule_id,),
            )
            row = cur.fetchone()
            return row[0] if row is not None else None
    except Exception:
        logger.exception("schedule {} run-record start failed", schedule_id)
        return None


def _record_run_end(database: Database, run_id: int | None, *, ok: bool, note: str | None) -> None:
    """Close a run-history row with its outcome. No-op when the start write
    failed (run_id is None); a failure here is likewise never fatal."""
    if run_id is None:
        return
    try:
        with database.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = %s, note = %s WHERE id = %s",
                (ok, note, run_id),
            )
    except Exception:
        logger.exception("schedule run-record end failed (run {})", run_id)


# A crashed run's note keeps this much of the traceback's END (the exception and the innermost
# frames): the session's output log is torn down with the session, and `schedules.last_error`
# holds only the latest crash, so this row is the history of why each run died.
_NOTE_TRACEBACK_MAX = 3000


def _crash_note(exc: BaseException, tb: str) -> str:
    tail = tb.strip()
    if len(tail) > _NOTE_TRACEBACK_MAX:
        tail = "[...truncated]\n" + tail[-_NOTE_TRACEBACK_MAX:]
    return f"crashed: {type(exc).__name__}\n{tail}"


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
    per `python -m services.wake.schedule_manager.runner <id>` session — but a
    ``.py`` schedule script runs in-process here, so the swap would leak into
    the rest of its run), which a later ``assert time.sleep is _REAL_SLEEP``
    guard in the test suite trips on. Idempotent and safe to call without a
    patch."""
    time.sleep = _ORIGINAL_SLEEP


def _stall_action(database: Database, schedule_id: int, message: str, run_id: int | None) -> None:
    """Reap descendants, record the failure within a fixed deadline, hard-exit 1."""
    try:
        try:
            # Snapshot descendants while ancestry still proves ownership. Retain
            # identities through TERM/KILL; never signal the shared PTY group.
            # setsid stays covered; already-reparented daemons are exempt.
            base.host.proc.kill_process_tree(os.getpid(), include_root=False)
        except Exception:
            logger.exception("Schedule {} child cleanup failed", schedule_id)
        logger.error("Schedule {} {}", schedule_id, message)
        # One interpreter-level deadline covers both writes: if the DB never
        # answers, faulthandler dumps every thread's stack (the hang's location)
        # and exits 1.
        faulthandler.dump_traceback_later(_FAILURE_RECORD_DEADLINE_S, exit=True)
        try:
            _record_error(database, schedule_id, message)
        except Exception:
            logger.opt(exception=True).warning(
                "Schedule {} stall message could not be recorded in last_error; exiting without it",
                schedule_id,
            )
        _record_run_end(database, run_id, ok=False, note=f"stalled ({_stall_timeout_s():.0f}s)")
    finally:
        os._exit(1)  # hard exit; the manager records the process loss


def _is_parked_frame(frame: FrameType) -> bool:
    """Recognize sleep-family parks and the actual subprocess wait operations."""
    if frame.f_code.co_name in _PARK_FRAME_NAMES:
        return True
    signature = (frame.f_code.co_filename, frame.f_code.co_name)
    if signature == (_SUBPROCESS_FILENAME, "_wait"):
        return True
    if signature != (_SELECTORS_FILENAME, "select"):
        return False
    parent = frame.f_back
    return parent is not None and (
        parent.f_code.co_filename,
        parent.f_code.co_name,
    ) == (_SUBPROCESS_FILENAME, "_communicate")


class _StallGuard:
    """Own frame observation and serialize script completion with stall admission.

    Sleep-family parks and actual subprocess waits retain their unlimited park
    contract. A child wait without timeout remains an accepted boundary. Spawn,
    argument conversion, stdin flush and selectors outside subprocess stay
    guarded. An unexpected guard failure is retained and raised by ``close``.
    """

    def __init__(self, database: Database, schedule_id: int, run_id: int | None) -> None:
        self.database = database
        self.schedule_id = schedule_id
        self.run_id = run_id
        self.main_thread_id = threading.get_ident()
        self.stop = threading.Event()
        self.admission = threading.Lock()
        self.action_started = False
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._guard, name=f"schedule-{schedule_id}-stall-guard", daemon=True
        )
        self.thread.start()

    def _guard(self) -> None:
        try:
            self._observe()
        except BaseException as exc:
            self.error = exc
            logger.opt(exception=exc).error("Schedule {} stall guard failed", self.schedule_id)

    def _sample(self) -> tuple[str, int, str] | None:
        frame = sys._current_frames().get(self.main_thread_id)
        if frame is None or _is_parked_frame(frame):
            return None
        return frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name

    def _claim_stall(self) -> bool:
        with self.admission:
            if self.stop.is_set():
                return False
            self.action_started = True
            return True

    def _observe(self) -> None:
        last_sig: tuple[str, int, str] | None = None
        stalled_since = 0.0
        while not self.stop.wait(_stall_check_interval_s()):
            sig = self._sample()
            now = time.monotonic()
            if sig is None or sig != last_sig:
                last_sig = sig
                stalled_since = now
                continue
            if now - stalled_since >= _stall_timeout_s():
                message = (
                    f"schedule runner stalled {now - stalled_since:.0f}s in "
                    f"{sig[2]} ({sig[0]}:{sig[1]}) — hard-exiting; check the "
                    "gateway / DB / network the script calls into"
                )
                if self._claim_stall():
                    _stall_action(self.database, self.schedule_id, message, self.run_id)
                return

    def close(self, timeout: float = 1.0) -> None:
        # Closing admission and claiming a real stall share one decision point.
        # Never hold this lock through cleanup or a synchronous database write.
        with self.admission:
            self.stop.set()
            action_started = self.action_started
        # Ordinary stop wakes immediately. An admitted action keeps the 3s TERM
        # + 5s reap and the record deadline before its hard exit.
        self.thread.join(timeout=_HARD_EXIT_WAIT_S if action_started else timeout)
        alive = self.thread.is_alive()
        if self.error is not None:
            raise self.error
        if alive:
            raise RuntimeError(f"Schedule {self.schedule_id} stall guard did not stop")


def _script_exit_code(exc: SystemExit) -> int:
    """Normalize a deliberate script exit with the interpreter's bool/message semantics."""
    return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)


def _record_script_exit(
    database: Database, schedule_id: int, run_id: int | None, exc: SystemExit
) -> int:
    """Record a .py script's deliberate sys.exit() like a command's exit code
    (QA P3-3 — it must not leave the run row in-progress forever). A None code
    is 0; a non-int code (a message string) is 1. int() normalizes bools (the
    interpreter treats them as exit codes: True -> 1, False -> 0); a non-int
    code is a message the interpreter would print to stderr, so it rides the
    note instead of being swallowed (QA N1)."""
    code = _script_exit_code(exc)
    if code == 0:
        _finish_completed(database, schedule_id, run_id)
    else:
        message = "" if isinstance(exc.code, int) else f": {exc.code}"
        _record_error(database, schedule_id, f"script exited {code}{message}")
        _record_run_end(database, run_id, ok=False, note=f"script exited {code}{message}")
    return code


def run(
    schedule_id: int,
    revision: int | None = None,
    *,
    database: Callable[[], Database],
    bind_actor: Callable[[int], None],
    load_plugins: Callable[[], None],
    inputs: ScheduleInputs,
) -> int:
    """Materialize + run the schedule. Returns a process exit code."""
    return _run(
        database(),
        schedule_id,
        revision,
        bind_actor=bind_actor,
        load_plugins=load_plugins,
        inputs=inputs,
    )


def _run_python_script(
    database: Database,
    schedule_id: int,
    run_id: int | None,
    script_path: Path,
    *,
    load_plugins: Callable[[], None],
    inputs: ScheduleInputs,
) -> None:
    """Run with isolated argv and collect the watchdog before any terminal write."""
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
    guard = _StallGuard(database, schedule_id, run_id)
    # The gateway launches this runner as `python -m services.wake.schedule_manager.runner
    # <id>`, so sys.argv carries the schedule id. The script must not
    # inherit that runner-only argv: hand it the argv `python <script>`
    # would produce — just its own path — and restore the runner's argv
    # afterwards (2026-09-22: the daily debt sweep's argparse rejected the
    # leaked id and exited 2 on every launch, tripping the crash breaker
    # before its first fire).
    runner_argv = sys.argv
    sys.argv = [str(script_path)]
    try:
        load_plugins()
        runpy.run_path(
            str(script_path), run_name="__main__", init_globals={"AVA_SCHEDULE_INPUTS": inputs}
        )
    finally:
        sys.argv = runner_argv
        primary = sys.exc_info()[1]
        try:
            guard.close()
        except BaseException:
            if primary is None or (
                isinstance(primary, SystemExit) and _script_exit_code(primary) == 0
            ):
                raise
            # Successful SystemExit is completion, so cleanup failure wins.
            # Preserve a failed script's primary failure; the guard keeps
            # its original error and already reported it immediately.
            logger.opt(exception=True).error("Schedule {} guard close failed", schedule_id)
        finally:
            _restore_park_detection()
            if guard.action_started:
                # A real stall won admission before script completion.
                # Never mark it completed, even if cleanup exhausted
                # the bounded join or a test replaced the hard exit.
                os._exit(1)


def _run(
    database: Database,
    schedule_id: int,
    revision: int | None = None,
    *,
    bind_actor: Callable[[int], None],
    load_plugins: Callable[[], None],
    inputs: ScheduleInputs,
) -> int:
    loaded = _load(database, schedule_id, revision)
    if loaded is None:
        logger.warning("Schedule {} is gone or disabled; nothing to run", schedule_id)
        return 0
    script, command = loaded

    work_dir = _schedule_dir(schedule_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    script_name = _script_filename(command)
    script_path = work_dir / script_name
    script_path.write_text(script)

    bind_actor(schedule_id)

    # Run history: one row per process execution, opened in-progress (ok=NULL)
    # here and closed with the outcome on every exit path below — including the
    # stall guard, which receives run_id and closes the row ok=false within its
    # bounded record deadline before hard exit; a deadline that expires abandons
    # the write. Both an abandoned write and a kill that leaves no code path to
    # close (SIGTERM/SIGHUP/SIGKILL) leave the row in-progress; the manager's
    # reconcile sweep closes it as 'interrupted' once the process is gone.
    run_id = _record_run_start(database, schedule_id)

    try:
        if script_name.endswith(".py"):
            _run_python_script(
                database, schedule_id, run_id, script_path, load_plugins=load_plugins, inputs=inputs
            )
            _finish_completed(
                database, schedule_id, run_id
            )  # clean return => finished, not crashed
            return 0
        # A non-.py command runs as a child process — the stall guard's main-
        # thread frame watch cannot see inside it, and the runner parked in
        # subprocess.run would read as a legitimate park anyway (a subprocess
        # frame marks a child wait). Bound it with the same stall timeout: a
        # command that has not finished within the budget is hung, not long-running —
        # without a bound, a never-exiting command would sit forever with no
        # last_error and no breaker fire, silently eating every future fire
        # window (2026-08-08 audit, P2-2 — the .py branch got its stall guard
        # after the 2026-08-03 self-evolution miss; the command branch was
        # still open). subprocess.run kills the child on expiry and raises
        # TimeoutExpired; the manager reports terminal process loss without replay.
        stall_timeout_s = _stall_timeout_s()
        try:
            result = subprocess.run(  # noqa: S603 — command is the operator-authored schedule command
                shlex.split(command),
                cwd=str(work_dir),
                check=False,
                timeout=stall_timeout_s,
            )
        except subprocess.TimeoutExpired:
            message = (
                f"command did not finish within {stall_timeout_s:.0f}s (stall timeout): {command!r}"
            )
            _record_error(database, schedule_id, message)
            logger.error("Schedule {} {}", schedule_id, message)
            _record_run_end(
                database, run_id, ok=False, note=f"stall timeout ({stall_timeout_s:.0f}s)"
            )
            return 1
        if result.returncode != 0:
            _record_error(database, schedule_id, f"command exited {result.returncode}: {command!r}")
            _record_run_end(database, run_id, ok=False, note=f"command exited {result.returncode}")
        else:
            _finish_completed(database, schedule_id, run_id)
        return result.returncode
    except SystemExit as exc:
        return _record_script_exit(database, schedule_id, run_id, exc)
    except Exception as exc:
        tb = traceback.format_exc()
        _record_error(database, schedule_id, tb)
        logger.error("Schedule runner execution failed: {}", tb)
        _record_run_end(database, run_id, ok=False, note=_crash_note(exc, tb))
        return 1
