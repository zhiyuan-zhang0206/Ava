"""Gateway-internal ScheduleManager — supervises one session per enabled schedule.

A schedule (the ``schedules`` table) is a supervised *resident* process: a script
+ a command, kept up by the manager. The gateway owns one ScheduleManager: a
single background task that every ``_POLL_INTERVAL_S`` reconciles *desired*
(enabled rows) against *actual* (live sessions named
``ava-schedule-<id>``), launching the missing ones and killing sessions
whose schedule was disabled/deleted.

Liveness is the session itself (its name is the schedule's stable identity),
so schedules survive a gateway restart — the manager re-adopts live sessions
instead of respawning them. Why a session vanished decides what happens next:

  - clean exit (rc=0): the runner records ``status='completed'`` before the
    session dies — a resident process that finished on its own. Terminal: the
    manager leaves it alone (no relaunch, no breaker). Re-run via start/restart.
  - crash (nonzero rc / signal / hard kill): no completed marker, so the manager
    relaunches with exponential backoff; past a launch ceiling the breaker trips
    and the schedule is left ``status='error'`` until re-enabled — this keeps a
    crash-looping schedule from respawning forever (and it never touches the
    agent-resurrect crash-loop signal the health probe watches, since a schedule
    restart is not an agent resurrect). The trip lives in the DB, not the
    manager's in-memory backoff, so reconcile treats ``status='error'`` as
    terminal (same as ``'completed'``) and does not relaunch it — a gateway
    restart, which wipes the in-memory counters, cannot resurrect a schedule the
    breaker already gave up on. Recovery is an explicit start/restart.

The manager reads liveness before status each tick, so a session seen dead is
guaranteed to already carry the runner's terminal write (completed / last_error).

Each tick also closes run-history rows the runner could not close itself: a
NULL (in-progress) row whose schedule has no live session belongs to a dead
process (a SIGKILLed stop/restart/edited-script save, an external kill, a
stale-session reap) and is closed as ``interrupted`` — a NULL row is
legitimate only while the schedule has a live session, so the run drawer
shows a permanent in-progress "…" only for a run that is actually running
(QA P2-2).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from shared.cluster import session_name
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.paths import ava_home, prod_service_checkout_error
from shared.session_backend import get_shell_backend
from shared.session_env import forward_env_dict

_log = logging.getLogger(__name__)

# Reconcile cadence. Snappier than cron (a schedule is a resident process, not a
# wall-clock fire), slower than the restarter's 1s — a few seconds of detection
# lag on a crash is fine and keeps the polling cheap.
_POLL_INTERVAL_S = 5.0

# Circuit breaker: a schedule launched more than _BREAKER_MAX times before it
# stays up trips the breaker (status='error', no more auto-launch). Backoff grows
# 2s, 4s, 8s, ... capped, between launches; a schedule that stays live past
# _STABLE_S after its last launch has its counter reset (it recovered).
_BREAKER_MAX = 5
_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 60.0
_STABLE_S = 60.0
_PTY_FAILURE_LOG_INTERVAL_S = 60.0

# Repo root (gateway/.. == <repo>/) — the cwd the runner launches from, so its
# relative `.venv/bin/python` resolves into this checkout's venv.
_REPO_ROOT = Path(__file__).resolve().parents[1]

_SCHEDULE_PREFIX = session_name("schedule-")  # ava-schedule-


class ScheduleManager:
    """Background reconcile loop for the ``schedules`` table. One per gateway,
    owned by the app lifespan: ``start()`` launches the task, ``stop()`` cancels
    it (schedule sessions are left running — they survive the gateway)."""

    def __init__(self, db_pool: ConnectionPool[Any]) -> None:
        self._pool = db_pool
        self._task: asyncio.Task[None] | None = None
        # schedule_id -> (launch_count, next-eligible monotonic time). launch_count
        # climbs on each (re)launch and resets after _STABLE_S of uptime.
        self._backoff: dict[int, tuple[int, float]] = {}
        # Enabled sessions whose stale same-name survivor resisted a reap. A
        # live session normally means adopt, but these need another official
        # reap before their replacement may launch.
        self._reap_retries: set[int] = set()
        # schedule id -> (last error-log monotonic time, suppressed failures).
        # A D-state survivor can resist each five-second reconcile forever; log
        # the first failure and periodic summaries rather than flooding ERROR.
        self._reap_failure_logs: dict[int, tuple[float, int]] = {}
        # Serializes the reconcile tick against API-driven control ops (restart),
        # which run on separate threads via asyncio.to_thread and both touch
        # backend + _backoff.
        self._lock = threading.Lock()

    async def start(self) -> None:
        # issue #194: a gateway launched from a foreign checkout (e.g. a dev
        # worktree against the prod home) must not supervise schedules — the
        # manager anchors each session to its own repo root, so its schedules
        # would run un-reviewed worktree code and die silently when the
        # worktree is removed. Same guard as `ava start` (Task #966).
        refusal = prod_service_checkout_error(_REPO_ROOT)
        if refusal is not None:
            _log.error("schedule supervision refused: {}", refusal)
            return
        self._task = asyncio.create_task(self._run(), name="schedule-manager")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # ── API-driven control (the /api/schedules/{id}/… routes call these) ────

    async def sync(self, schedule_id: int) -> None:
        """Converge one schedule's session to its DB `enabled` state right
        now (kill it, then relaunch if enabled), clearing its crash backoff. This
        is the immediate path behind start / stop / restart and an edited-script
        save (the relaunch makes the runner re-materialize the new script) —
        rather than waiting up to a poll interval for the reconcile loop."""
        await asyncio.to_thread(self._sync_blocking, schedule_id)

    def _sync_blocking(self, schedule_id: int) -> None:
        from shared import maintenance

        if maintenance.held():
            return
        with self._lock:
            self._backoff.pop(schedule_id, None)
            if not self._reap(schedule_id):
                if schedule_id in self._load_enabled():
                    self._reap_retries.add(schedule_id)
                return
            if schedule_id in self._load_enabled():
                self._launch(schedule_id)

    async def capture(self, schedule_id: int, lines: int) -> str | None:
        """The schedule session's recent output, or None when
        no session is live."""
        return await asyncio.to_thread(self._capture_blocking, schedule_id, lines)

    def _capture_blocking(self, schedule_id: int, lines: int) -> str | None:
        name = session_name(f"schedule-{schedule_id}")
        backend = get_shell_backend()
        if not backend.has_session(name):
            return None
        return backend.capture_pane(name, lines)

    async def _run(self) -> None:
        _log.info("schedule manager started (reconcile every %ss)", _POLL_INTERVAL_S)
        while True:
            try:
                await asyncio.to_thread(self._reconcile)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick (DB blip / backend hiccup) must not kill the loop.
                _log.exception("schedule manager reconcile failed")
            await asyncio.sleep(_POLL_INTERVAL_S)

    # ── reconcile (blocking: session backend + DB, run via asyncio.to_thread) ──

    def _reconcile(self) -> None:
        with self._lock:
            self._reconcile_locked()

    def _reconcile_locked(self) -> None:
        from shared import maintenance

        if maintenance.held():
            return
        # Read liveness before status: on a clean exit the runner commits
        # status='completed' before its process exits (and the session dies), so a
        # session seen dead here is guaranteed to already carry its terminal status.
        retrying = self._reap_retries.copy()
        live = self._live_ids()  # {id}
        status = self._load_enabled()  # {id: status}
        enabled = set(status)
        known = live | enabled
        self._reap_retries.intersection_update(known)
        self._reap_failure_logs = {
            schedule_id: state
            for schedule_id, state in self._reap_failure_logs.items()
            if schedule_id in known
        }
        now = time.monotonic()
        from shared import start_serving

        # A launch can find a same-name session after liveness initially said it
        # was absent. If the official reap refused it, this set makes the next
        # reconcile retry rather than adopting the stale survivor forever.
        retrying.intersection_update(live)
        for sid in retrying:
            if self._reap(sid):
                live.remove(sid)

        # Kill sessions we no longer want (disabled / deleted).
        for sid in live - enabled:
            self._backoff.pop(sid, None)
            if sid not in retrying:
                self._reap(sid)

        # (Re)launch enabled schedules with no live session — unless the row is in
        # a terminal status that must not be auto-launched:
        #   - 'completed': a resident process that finished on its own.
        #   - 'error': the breaker already tripped. This is the DB, not memory, so
        #     the trip survives a gateway restart — a crash-looping schedule that
        #     already tripped is not relaunched with a fresh counter (the
        #     in-memory self._backoff is empty after a restart). Recovery is an
        #     explicit API restart/start, which relaunches and resets status.
        for sid in enabled - live:
            if status[sid] in ("completed", "error"):
                self._backoff.pop(sid, None)  # terminal — clear any prior backoff
                continue
            count, deadline = self._backoff.get(sid, (0, 0.0))
            if now < deadline:
                continue  # backing off
            if count >= _BREAKER_MAX:
                self._trip_breaker(sid, count)
                continue
            with start_serving.recovery_permitted() as permitted:
                if not permitted:
                    continue
                self._launch(sid)
                delay = min(_BACKOFF_BASE_S * 2**count, _BACKOFF_CAP_S)
                self._backoff[sid] = (count + 1, now + delay)

        # Reset backoff for schedules that have stayed live past the stable window.
        for sid in enabled & live:
            count, deadline = self._backoff.get(sid, (0, 0.0))
            if count > 0 and now > deadline + _STABLE_S:
                self._backoff.pop(sid, None)

        # Close run rows the runner could not close: a NULL row is legitimate
        # only while the schedule has a live session (QA P2-2). Launches above
        # already closed their dead predecessors; this catches every other
        # orphan (disabled/terminal schedules, backing-off relaunches, legacy
        # rows from before this sweep existed).
        self._close_orphan_runs()

    def _live_ids(self) -> set[int]:
        from shared.pty_sessions.allocation_freeze import current_generation

        live: set[int] = set()
        backend = get_shell_backend()
        generation = current_generation()
        names = backend.list_sessions(prefix=_SCHEDULE_PREFIX)
        home = ava_home()
        _log.debug(
            "schedule session scan backend=%s settings_home=%s record_dir=%s names=%s",
            type(backend).__name__,
            home,
            home / "run" / "pty",
            names,
        )
        for name in names:
            tail = name.removeprefix(_SCHEDULE_PREFIX)
            if tail.isdigit():
                schedule_id = int(tail)
                if backend.session_generation(name) == generation:
                    live.add(schedule_id)
                    continue
                # An enabled schedule is current desired state, but a matching
                # name from the preceding flip is not its current exact
                # session. Reap the old record and let this tick launch the
                # desired runner under the active generation.
                if schedule_id in self._reap_retries:
                    live.add(schedule_id)
                    continue
                _log.warning(
                    "schedule %s: reaping superseded generation session %s",
                    tail,
                    name,
                )
                if not self._reap(schedule_id):
                    self._reap_retries.add(schedule_id)
                    # Do not launch a second runner while the old exact session
                    # is still live. The centralized reaper retries with
                    # rate-limited failure accounting on the next tick.
                    live.add(schedule_id)
        return live

    def _close_null_runs(self, schedule_id: int) -> None:
        """Close a schedule's in-progress run-history rows (ok IS NULL) as
        'interrupted' — the runner that opened them is gone. Best-effort (run
        history is severable observability) and idempotent (WHERE ok IS NULL):
        a row the runner closed itself, or one already swept, is untouched."""
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = false, note = 'interrupted' "
                "WHERE schedule_id = %s AND ok IS NULL",
                (schedule_id,),
            )

    def _close_orphan_runs(self) -> None:
        """Close every in-progress run row whose schedule has no live session.

        The runner closes its row on every exit path it can reach; a row still
        NULL after its process died — the manager's own stop/restart/edited-
        script save kills with SIGKILL, an external kill, a gateway restart's
        stale-session reap, a crash between the row open and any close — would
        otherwise render as in-progress forever (QA P2-2). The manager owns
        session liveness, so it closes them: history is preserved as a visible
        ✗ interrupted row instead of an eternal "…". Schedules launched in
        this tick already had their rows closed at launch; this catches the
        rest (disabled, terminal, backing off, never launched)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT schedule_id FROM schedule_runs WHERE ok IS NULL")
            ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return
        live = self._live_ids()
        for sid in ids:
            if sid not in live:
                self._close_null_runs(sid)

    def _launch(self, schedule_id: int) -> None:
        from shared import maintenance

        if maintenance.held():
            return
        name = session_name(f"schedule-{schedule_id}")
        backend = get_shell_backend()
        # A gateway restart (rollout / crash) can leave the previous run's
        # session behind — the old runner process keeps serving until its PTY
        # host reaps it. The PTY backend's new_session is idempotent
        # (a live same-name session is left untouched), but a stale record or
        # a still-serving runner must not shadow the launch, so reap a
        # same-name survivor first (#1119, 2026-08-09 rollout). _launch is only
        # ever reached for a schedule the manager does not consider live (the
        # reconcile loop adopts live sessions without relaunching), so a
        # same-name survivor is by definition stale.
        if name in backend.list_sessions():
            _log.warning("schedule %s: reaping stale session %s", schedule_id, name)
            if not self._reap(schedule_id):
                self._reap_retries.add(schedule_id)
                return
        # _launch is only reached for a schedule with no live session, so any
        # in-progress run row at this point belongs to a dead process (the
        # reaped survivor, an externally killed runner, a crash that skipped
        # its own close). Close those rows now — the new process opens a fresh
        # row, and without this the old one would render as in-progress beside
        # it forever (QA P2-2).
        self._close_null_runs(schedule_id)
        # The unit's config + this run's schedule id ride a 0600 env file the
        # session sources; the backend writes it from the env dict (never argv
        # — issue #974). The backend hands the command to the per-session host,
        # which submits it once the login shell is ready.
        env = forward_env_dict()
        env["AVA_SCHEDULE_ID"] = str(schedule_id)
        # The runner's cron math is timezone-DEFINED: it must fire on the
        # CLUSTER's clock, not on whatever ambient env the session happens to
        # carry. forward_env_dict deliberately excludes cluster-scope keys (a
        # child is expected to re-source .env at boot), but a unit .env that
        # misses AVA_TIMEZONE silently left the runner on the field default
        # America/Los_Angeles — 2026-08-21: schedule #3 fired at PT midnight
        # instead of Shanghai midnight after the 08-12 timezone ruling. Pin the
        # gateway's own resolved timezone into the spawn env so the fire time is
        # deterministic; dotenv_boot's authority pass still lets a declared .env
        # value override it (and never drops it — shared/dotenv_boot._force_also).
        env["AVA_TIMEZONE"] = settings.general.timezone
        # `cd` first: the login shell's own profile can move the cwd the
        # daemon forked with (macOS path_helper rebuilds PATH; a profile `cd`
        # would strand the relative runner path). The `; exit $?` tail makes
        # the shell end with the runner, so a dead runner never leaves a live
        # session behind for the reconcile's session-existence liveness to
        # misread (the Task #1115 bug-B class).
        cmd = (
            f"cd {shlex.quote(str(_REPO_ROOT))} && "
            f".venv/bin/python -m gateway.schedule_runner {schedule_id}; exit $?"
        )
        try:
            backend.new_session(name, cmd, _REPO_ROOT, env=env)
        except Exception as exc:
            # A supervisor/daemon failure must not kill the reconcile tick; the
            # breaker's next launch attempt backs off and retries.
            _log.error("schedule %s launch failed: %s", schedule_id, exc)
            return
        if not backend.has_session(name):
            _log.error(
                "schedule %s launch failed: session %s did not come up "
                "(daemon down?) — breaker will retry",
                schedule_id,
                name,
            )
            return
        self._reap_retries.discard(schedule_id)
        _log.info("schedule %s launched (session %s)", schedule_id, name)
        # A successful launch clears any stale breaker-trip text: the row was
        # left in 'error' by a prior crash loop, then recovered via an explicit
        # restart -- without this, last_error keeps showing "auto-restart
        # paused" forever even though the schedule is running fine (2026-08-11,
        # all 4 schedules had stale text after a rollout crash-loop).
        self._set_status(schedule_id, "running", clear_last_error=True)

    def _log_reap_failure(
        self, schedule_id: int, name: str, failure: str, *, exc_info: bool = False
    ) -> None:
        """Log an unresolved reap at most once per schedule per cooldown."""
        now = time.monotonic()
        prior = self._reap_failure_logs.get(schedule_id)
        if prior is not None:
            last_logged, suppressed = prior
            if now - last_logged < _PTY_FAILURE_LOG_INTERVAL_S:
                self._reap_failure_logs[schedule_id] = (last_logged, suppressed + 1)
                return
        else:
            suppressed = 0
        self._reap_failure_logs[schedule_id] = (now, 0)
        summary = "" if suppressed == 0 else f" (suppressed {suppressed} repeats)"
        _log.error(
            "schedule %s: PTY reap %s for session %s%s",
            schedule_id,
            failure,
            name,
            summary,
            exc_info=exc_info,
        )

    def _reap(self, schedule_id: int) -> bool:
        """Reap one schedule PTY through its identity-checked backend.

        The backend's success result means the session is confirmed gone. Do
        not record a stopped schedule or close its in-progress run before that
        confirmation: disabled rows with a surviving PTY remain in the
        reconcile set and receive another official reap attempt next tick.
        """
        name = session_name(f"schedule-{schedule_id}")
        try:
            reaped, mode = get_shell_backend().kill_session(name)
        except Exception:
            self._log_reap_failure(schedule_id, name, "raised", exc_info=True)
            return False
        if not reaped:
            # SessionBackend's false verdict means the session survived its
            # own identity-aware kill. Keep metadata truthful until reconcile
            # can try the same official path again.
            self._set_status(schedule_id, "running")
            self._log_reap_failure(schedule_id, name, "survived")
            return False
        self._reap_retries.discard(schedule_id)
        self._reap_failure_logs.pop(schedule_id, None)
        self._set_status(schedule_id, "stopped")
        # The killed process can never close its own run rows (the kill chain
        # is SIGKILL — no handler runs); close them now so a stop/restart/
        # edited-script save does not leave an in-progress "…" row behind (QA
        # P2-2). The next reconcile sweep is the backstop for kills that do
        # not route through here.
        self._close_null_runs(schedule_id)
        _log.info("schedule %s: PTY session %s reaped (%s)", schedule_id, name, mode)
        return True

    def _trip_breaker(self, schedule_id: int, count: int) -> None:
        # Only write once per trip: flip to error the first time we cross the ceiling.
        updated = self._set_status(
            schedule_id,
            "error",
            last_error=(
                f"auto-restart paused: schedule crashed {count} times without staying up "
                "(a clean exit is recorded as completed, not error); "
                "fix the script and re-enable to retry"
            ),
            only_if_not_error=True,
        )
        if updated:
            _log.warning("schedule %s breaker tripped after %s launches", schedule_id, count)

    # ── DB ──────────────────────────────────────────────────────────────────

    def _load_enabled(self) -> dict[int, str]:
        """Enabled schedules as ``{id: status}`` — the reconcile loop needs the
        status to leave a cleanly-exited ('completed') schedule alone."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, status FROM schedules WHERE enabled = true")
            return {r[0]: r[1] for r in cur.fetchall()}

    def _set_status(
        self,
        schedule_id: int,
        status: str,
        *,
        last_error: str | None = None,
        only_if_not_error: bool = False,
        clear_last_error: bool = False,
    ) -> bool:
        """Update a schedule's status (and optionally last_error). With
        only_if_not_error, skip a row already in 'error' so the breaker logs its
        trip exactly once. `clear_last_error` nulls last_error alongside the
        status flip (a recovered launch must not keep stale breaker-trip text).
        Returns whether a row changed."""
        # Five fully-static queries selected by the flags — no string building.
        if last_error is not None:
            params: tuple[object, ...] = (status, last_error, schedule_id)
            sql = (
                "UPDATE schedules SET status=%s, last_error=%s, updated_at=now() "
                "WHERE id=%s AND status <> 'error'"
                if only_if_not_error
                else "UPDATE schedules SET status=%s, last_error=%s, updated_at=now() WHERE id=%s"
            )
        elif clear_last_error:
            params = (status, schedule_id)
            sql = (
                "UPDATE schedules SET status=%s, last_error=NULL, updated_at=now() "
                "WHERE id=%s AND status <> 'error'"
                if only_if_not_error
                else "UPDATE schedules SET status=%s, last_error=NULL, updated_at=now() WHERE id=%s"
            )
        else:
            params = (status, schedule_id)
            sql = (
                "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s AND status <> 'error'"
                if only_if_not_error
                else "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s"
            )
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
            return cur.rowcount > 0
