"""Admit one schedule session per explicit desired revision.

The manager adopts matching live PTYs, reaps obsolete sessions through the
identity-checked backend and consumes schedule_sync_requests. It commits a
one-attempt launch latch before creating the process. Lost or ambiguous starts
are errors requiring explicit start/restart; no crash retry or breaker remains.
The latch and the two-hour absence alert clock survive service restart.
Run-history rows left open by a dead process are closed when the manager next
launches or reaps that schedule's session, not by a periodic sweep.

Convergence that keeps failing is bounded: a reap that survives, or a live
session whose launch provenance cannot be read, is retried for at most
``_BLOCKED_GIVE_UP_AFTER_S`` of one failure streak. The manager then stops,
records the schedule as an error, emits ``schedule_convergence_abandoned`` and
leaves the process alone until an explicit new revision (start, stop, restart,
edit, delete) authorizes another attempt.
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base import telemetry
from base.clock import Clock
from base.cluster import session_name
from base.daemon.schedules.timing import SCHEDULE_STALL_ALERT_AFTER_S
from base.db.transaction import write_transaction
from base.paths import ava_home
from base.sessions.backend import SessionBackend, get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from services.wake.schedule_manager import revisions

_log = logging.getLogger(__name__)

# Reconcile cadence. Snappier than cron (a schedule is a resident process, not a
# wall-clock fire) — a few seconds of detection
# lag on a crash is fine and keeps the polling cheap.
POLL_INTERVAL_S = 5.0

_PTY_FAILURE_LOG_INTERVAL_S = 60.0
_STALL_ALERT_AFTER_S = SCHEDULE_STALL_ALERT_AFTER_S

# One streak of failed convergence attempts for a schedule ends in a give-up after
# this long. Two failures further apart than the gap (a maintenance hold or a
# paused service sits between them) are not one streak.
_BLOCKED_GIVE_UP_AFTER_S = 600.0
_BLOCKED_STREAK_GAP_S = 60.0

# Repo root (services/wake/schedule_manager/../../.. == <repo>/) — the cwd the runner launches from, so its
# relative `.venv/bin/python` resolves into this checkout's venv.
REPO_ROOT = Path(__file__).resolve().parents[3]


class ScheduleManager:
    """The reconcile logic for the ``schedules`` table. One per cluster, owned by
    the ``schedule-manager`` service, whose loops call `reconcile` and
    `sync_one`. Launch admission and the stall-alert clock are columns of the
    schedule row; schedule sessions are never touched when the service stops
    (they survive it)."""

    def __init__(self, db_pool: ConnectionPool[Any]) -> None:
        self._pool = db_pool
        # Enabled sessions whose stale same-name survivor resisted a reap. A
        # live session normally means adopt, but these need another official
        # reap before their replacement may launch. In memory: a restarted
        # service meets the same survivor again (a session of another generation
        # is reaped by `_live_ids` itself).
        self._reap_retries: set[int] = set()
        # schedule id -> (last error-log monotonic time, suppressed failures).
        # A D-state survivor can resist each five-second reconcile forever; log
        # the first failure and periodic summaries rather than flooding ERROR.
        self._reap_failure_logs: dict[int, tuple[float, int]] = {}
        # schedule id -> (first, latest) monotonic time of the current streak of
        # failed convergence attempts.
        self._blocked: dict[int, tuple[float, float]] = {}
        # schedule id -> desired revision at the moment the manager gave up. The
        # process is left alone until the desired revision changes (an explicit
        # start, stop, restart, edit or delete); in memory, so a restarted service
        # starts one new bounded streak.
        self._abandoned: dict[int, int | None] = {}
        # Serializes the reconcile tick against the sync requests, which run on
        # separate threads and both touch the backend and launch-admission columns.
        self._lock = threading.Lock()

    # ── control (the sync-request consumer calls this) ──────────────────────

    def sync_one(self, schedule_id: int) -> bool:
        """Converge the current durable desired revision, preserving an applied run.

        Return False while held or while the attempt is blocked (a surviving
        session, missing safe session provenance); the durable request stays
        queued for the next try. A blocked streak is bounded: once the manager
        gives up it returns True, so the request is consumed and not retried.
        """
        from base.deploy.maintenance import admission

        if admission.held():
            return False
        with self._lock:
            converged = self._sync_revision(schedule_id)
            if converged:
                self._blocked.pop(schedule_id, None)
            return converged or schedule_id in self._abandoned

    def _sync_revision(self, schedule_id: int) -> bool:
        if self._is_abandoned(schedule_id):
            return False
        with revisions.claim_convergence(self._pool, schedule_id) as owned:
            if not owned:
                return False
            return self._sync_owned_revision(schedule_id)

    def _sync_owned_revision(self, schedule_id: int) -> bool:
        wanted = revisions.desired(self._pool, schedule_id)
        if wanted is None or not wanted.enabled:
            if not self._reap(schedule_id, owned=True):
                return False
            if wanted is not None:
                revisions.mark_applied(self._pool, schedule_id, wanted.revision)
            return True
        if schedule_id in self._live_ids(owned_id=schedule_id):
            observed = revisions.launched_revision(schedule_id)
            if observed is None:
                # Existing legacy sessions at revision zero are adopted. For a
                # pending versioned command missing provenance means uncertain,
                # not permission to kill a possibly already-applied execution.
                legacy = wanted.revision == wanted.applied == 0
                if not legacy:
                    self._note_blocked(
                        schedule_id, "launch provenance of the live session is unreadable"
                    )
                return legacy
            if observed == wanted.revision:
                self._mark_running(schedule_id, wanted.revision)
                return True
            if not self._reap(schedule_id, owned=True):
                self._reap_retries.add(schedule_id)
                return False
        elif wanted.revision == wanted.applied and wanted.status in (
            "running",
            "completed",
            "error",
        ):
            if wanted.status == "running":
                self._mark_lost(schedule_id, wanted.revision)
            return True
        return self._launch_revision(schedule_id)

    def _launch_revision(self, schedule_id: int) -> bool:
        from base.deploy.lifecycle import start_serving

        with start_serving.recovery_permitted() as permitted:
            if not permitted:
                return False
            self._launch(schedule_id)
        latest = revisions.desired(self._pool, schedule_id)
        return latest is not None and latest.revision == latest.applied

    # ── reconcile (blocking: session backend + DB, run via asyncio.to_thread) ──

    def reconcile(self) -> None:
        with self._lock:
            self._reconcile_locked()

    def _reconcile_locked(self) -> None:
        from base.deploy.maintenance import admission

        if admission.held():
            return
        # A given-up schedule is not retried: only a new revision lifts that.
        self._reap_retries.difference_update(self._abandoned)
        retrying = self._reap_retries.copy()
        # Read liveness before status: on a clean exit the runner commits
        # status='completed' before its process exits (and the session dies), so a
        # session seen dead here is guaranteed to already carry its terminal status.
        live = self._live_ids()  # {id}
        enabled_rows = self._load_enabled()
        enabled = set(enabled_rows)
        known = live | enabled
        self._forget_unknown(known)
        self._report_stalled_schedules(enabled_rows, live)
        # A launch can find a same-name session after liveness initially said it
        # was absent. If the official reap refused it, this set makes the next
        # reconcile retry rather than adopting the stale survivor forever.
        retrying.intersection_update(live)
        for sid in retrying:
            if self._retry_reap(sid):
                live.remove(sid)

        # Kill sessions we no longer want (disabled / deleted).
        orphans = live - enabled
        for sid in orphans:
            if sid not in retrying:
                self._sync_revision(sid)

        for sid in sorted(enabled):
            wanted = revisions.desired(self._pool, sid)
            if wanted is not None and (
                wanted.revision != wanted.applied or (sid in live and wanted.status == "error")
            ):
                self._sync_revision(sid)
        live = self._live_ids()
        enabled_rows = self._load_enabled()
        self._launch_missing(enabled_rows, sorted(set(enabled_rows) - live))

    def _forget_unknown(self, known: set[int]) -> None:
        """Drop per-schedule retry state of schedules that are neither live nor enabled."""
        self._reap_retries.intersection_update(known)
        self._reap_failure_logs = {
            sid: state for sid, state in self._reap_failure_logs.items() if sid in known
        }
        self._blocked = {sid: span for sid, span in self._blocked.items() if sid in known}
        self._abandoned = {sid: rev for sid, rev in self._abandoned.items() if sid in known}

    def _launch_missing(self, enabled_rows: dict[int, str], missing: list[int]) -> None:
        """Apply explicit revisions; an admitted missing execution is never relaunched."""
        for sid in missing:
            if enabled_rows[sid] not in ("completed", "error"):
                self._sync_revision(sid)

    def _mark_lost(self, schedule_id: int, revision: int) -> None:
        with write_transaction(self._pool) as conn:
            changed = conn.execute(
                "UPDATE schedules SET status = 'error', last_error = %s, updated_at = now() "
                "WHERE id = %s AND desired_revision = %s AND applied_revision = %s "
                "AND enabled AND status = 'running'",
                (
                    "Schedule process lost; inspect effects and explicitly restart to run again",
                    schedule_id,
                    revision,
                    revision,
                ),
            ).rowcount
        if changed:
            _log.warning("schedule %s process lost; automatic relaunch disabled", schedule_id)

    def _report_stalled_schedules(self, status_by_id: dict[int, str], live_ids: set[int]) -> None:
        """Alert once when an active schedule has been sessionless for 2h.

        The clock is the row's ``not_live_since``; the alert claims
        ``stall_alerted_at`` in the same statement that finds it due, so one
        outage alerts once even across restarts. A live, completed or disabled
        schedule has both cleared, which rearms a later outage."""
        eligible = {sid for sid, status in status_by_id.items() if status != "completed"}
        missing = sorted(eligible - live_ids)
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET not_live_since = NULL, stall_alerted_at = NULL "
                "WHERE NOT (id = ANY(%s)) "
                "AND (not_live_since IS NOT NULL OR stall_alerted_at IS NOT NULL)",
                (missing,),
            )
            cur.execute(
                "UPDATE schedules SET not_live_since = clock_timestamp() "
                "WHERE id = ANY(%s) AND not_live_since IS NULL",
                (missing,),
            )
            cur.execute(
                "UPDATE schedules SET stall_alerted_at = clock_timestamp() "
                "WHERE id = ANY(%s) AND stall_alerted_at IS NULL "
                "AND not_live_since < clock_timestamp() - make_interval(secs => %s) "
                "RETURNING id, EXTRACT(EPOCH FROM clock_timestamp() - not_live_since)",
                (missing, _STALL_ALERT_AFTER_S),
            )
            alerts = [(int(r[0]), float(r[1])) for r in cur.fetchall()]
        for schedule_id, stalled_seconds in alerts:
            rounded_seconds = round(stalled_seconds, 3)
            status = status_by_id[schedule_id]
            _log.warning(
                "schedule %s has had no live session for %.1fs (status=%s)",
                schedule_id,
                rounded_seconds,
                status,
            )
            telemetry.emit(
                "telemetry",
                event_name="schedule_stalled",
                level="warning",
                attributes={
                    "schedule_id": schedule_id,
                    "status": status,
                    "stalled_seconds": rounded_seconds,
                },
            )

    def _live_ids(self, *, owned_id: int | None = None) -> set[int]:
        from base.sessions.pty.allocation_freeze import current_generation

        live: set[int] = set()
        backend = get_shell_backend()
        generation = current_generation()
        prefix = session_name("schedule-")  # ava-schedule-
        names = backend.list_sessions(prefix=prefix)
        home = ava_home()
        _log.debug(
            "schedule session scan backend=%s settings_home=%s record_dir=%s names=%s",
            type(backend).__name__,
            home,
            home / "run" / "pty",
            names,
        )
        for name in names:
            tail = name.removeprefix(prefix)
            if tail.isdigit():
                schedule_id = int(tail)
                observed_generation = backend.session_generation(name)
                if observed_generation == generation:
                    live.add(schedule_id)
                    continue
                # An enabled schedule is current desired state, but a matching
                # name from the preceding flip is not its current exact
                # session. Reap the old record; an admitted revision stays
                # consumed, while an explicit new revision may launch.
                if owned_id is not None and owned_id != schedule_id:
                    live.add(schedule_id)
                    continue
                if schedule_id in self._reap_retries or schedule_id in self._abandoned:
                    live.add(schedule_id)
                    continue
                _log.warning(
                    "schedule %s: reaping superseded generation session %s",
                    tail,
                    name,
                )
                if not self._reap(
                    schedule_id,
                    owned=owned_id == schedule_id,
                    expected_generation=observed_generation,
                ):
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
        a row the runner closed itself is untouched. Called when the manager
        launches or reaps the schedule, the only moments it knows the process
        is gone; a process killed by something else leaves its row open until
        then."""
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = false, note = 'interrupted' "
                "WHERE schedule_id = %s AND ok IS NULL",
                (schedule_id,),
            )

    def _launch(self, schedule_id: int) -> None:
        from base.deploy.maintenance import admission

        if admission.held():
            return
        wanted = revisions.desired(self._pool, schedule_id)
        if wanted is None or not wanted.enabled:
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
        if not self._prepare_launch(schedule_id, name, backend, wanted):
            return
        if not self._claim_launch(schedule_id, wanted.revision):
            return
        # _launch is only reached for a schedule with no live session, so any
        # in-progress run row at this point belongs to a dead process (the
        # reaped survivor, an externally killed runner, a crash that skipped
        # its own close). Close those rows now — the new process opens a fresh
        # row, and without this the old one would render as in-progress beside
        # it forever (QA P2-2).
        self._close_null_runs(schedule_id)
        # The unit's config + this run's schedule id ride the env dict, which the
        # backend sends in the request body of the pty-sessions service's 0600
        # socket (never argv — issue #974). The service submits the command once
        # the login shell is ready.
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
        # value override it (and never drops it — base/dotenv_boot._force_also).
        env["AVA_TIMEZONE"] = Clock.from_settings().timezone
        # `cd` first: the login shell's own profile can move the cwd the
        # daemon forked with (macOS path_helper rebuilds PATH; a profile `cd`
        # would strand the relative runner path). The `; exit $?` tail makes
        # the shell end with the runner, so a dead runner never leaves a live
        # session behind for the reconcile's session-existence liveness to
        # misread (the Task #1115 bug-B class).
        cmd = (
            f"cd {shlex.quote(str(REPO_ROOT))} && "
            f".venv/bin/python -m services.wake.schedule_manager.runner {schedule_id} {wanted.revision}; exit $?"
        )
        # No shell-TTL row is written here (deliberate exemption, task
        # #2614): agent_shell_ttls rows key on an agent id, and schedule
        # sessions have none (ava-schedule-<id> is allocated outside the
        # per-agent session counter). The ScheduleManager is their reclaimer
        # — reconcile reaps disabled/deleted schedules — and a registered 24h
        # deadline would make the TTL reaper kill a resident schedule every
        # day, one kill/relaunch cycle per schedule.
        try:
            backend.new_session(name, cmd, REPO_ROOT, env=env)
        except Exception as exc:
            # Admission was committed before the request. Its unknown outcome
            # consumes this revision; only an explicit restart may try again.
            _log.error("schedule %s launch failed: %s", schedule_id, exc)
            return
        if not backend.has_session(name):
            _log.error(
                "schedule %s launch failed: session %s did not come up "
                "(daemon down?) — explicit restart required",
                schedule_id,
                name,
            )
            return
        self._reap_retries.discard(schedule_id)
        _log.info("schedule %s launched (session %s)", schedule_id, name)
        self._mark_running(schedule_id, wanted.revision)

    def _mark_running(self, schedule_id: int, revision: int) -> None:
        """Confirm a matching live process without overwriting completion or newer intent."""
        with write_transaction(self._pool) as conn:
            conn.execute(
                "UPDATE schedules SET applied_revision = %s, status = CASE "
                "WHEN applied_revision = %s AND status = 'completed' THEN status ELSE 'running' END, "
                "last_error = NULL WHERE id = %s AND desired_revision = %s AND enabled",
                (revision, revision, schedule_id, revision),
            )

    def _prepare_launch(
        self, schedule_id: int, name: str, backend: SessionBackend, wanted: revisions.Desired
    ) -> bool:
        from base.sessions.pty.allocation_freeze import current_generation

        if name not in backend.list_sessions():
            return True
        if backend.session_generation(name) == current_generation():
            observed = revisions.launched_revision(schedule_id)
            if observed == wanted.revision:
                self._mark_running(schedule_id, wanted.revision)
                return False
            if observed is None:
                if wanted.revision > 0:
                    return False  # uncertain allocation: no authority to replace
                if wanted.applied == 0 and wanted.status == "running":
                    return False  # legacy healthy execution observed after a stale scan
        _log.warning("schedule %s: reaping stale session %s", schedule_id, name)
        if not self._reap(schedule_id, owned=True):
            self._reap_retries.add(schedule_id)
            return False
        return True

    def _is_abandoned(self, schedule_id: int) -> bool:
        """Whether the manager gave up on this schedule at its current revision.

        A changed desired revision (or a deleted row) is a new explicit request:
        it clears the give-up and authorizes a fresh bounded streak."""
        if schedule_id not in self._abandoned:
            return False
        wanted = revisions.desired(self._pool, schedule_id)
        if (None if wanted is None else wanted.revision) != self._abandoned[schedule_id]:
            del self._abandoned[schedule_id]
            self._blocked.pop(schedule_id, None)
            return False
        return True

    def _note_blocked(self, schedule_id: int, why: str) -> None:
        """Count one failed convergence attempt; give up when the streak is old enough."""
        now = time.monotonic()
        first, latest = self._blocked.get(schedule_id, (now, now))
        if now - latest > _BLOCKED_STREAK_GAP_S:
            first = now
        self._blocked[schedule_id] = (first, now)
        if now - first >= _BLOCKED_GIVE_UP_AFTER_S:
            self._give_up(schedule_id, why, now - first)

    def _give_up(self, schedule_id: int, why: str, blocked_s: float) -> None:
        """Stop retrying: record the schedule as an error, alert, leave the process alone."""
        wanted = revisions.desired(self._pool, schedule_id)
        self._abandoned[schedule_id] = None if wanted is None else wanted.revision
        self._reap_retries.discard(schedule_id)
        self._blocked.pop(schedule_id, None)
        message = (
            f"Convergence gave up after {blocked_s:.0f}s: {why}; inspect the process, "
            "stop it by hand if needed, then explicitly restart"
        )
        with write_transaction(self._pool) as conn:
            conn.execute(
                "UPDATE schedules SET status = 'error', last_error = %s, updated_at = now() "
                "WHERE id = %s",
                (message, schedule_id),
            )
        _log.error("schedule %s: %s", schedule_id, message)
        telemetry.emit(
            "telemetry",
            event_name="schedule_convergence_abandoned",
            level="warning",
            attributes={
                "schedule_id": schedule_id,
                "reason": why,
                "blocked_seconds": round(blocked_s, 3),
            },
        )

    def _log_reap_failure(
        self, schedule_id: int, name: str, failure: str, *, exc: BaseException | None = None
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
            exc_info=exc,
        )

    def _retry_reap(self, schedule_id: int) -> bool:
        with revisions.claim_convergence(self._pool, schedule_id) as owned:
            if not owned:
                return False
            wanted = revisions.desired(self._pool, schedule_id)
            observed = revisions.launched_revision(schedule_id)
            if wanted is not None and observed == wanted.revision == wanted.applied:
                self._reap_retries.discard(schedule_id)
                return False  # a different owner already installed this revision
            return self._reap(schedule_id, owned=True)

    def _reap(
        self, schedule_id: int, *, owned: bool = False, expected_generation: str | None = None
    ) -> bool:
        if owned:
            return self._reap_owned(schedule_id, expected_generation)
        with revisions.claim_convergence(self._pool, schedule_id) as claimed:
            if not claimed:
                return False
            return self._reap_owned(schedule_id, expected_generation)

    def _reap_owned(self, schedule_id: int, expected_generation: str | None = None) -> bool:
        """Reap one schedule PTY through its identity-checked backend.

        The backend's success result means the session is confirmed gone. Do
        not record a stopped schedule or close its in-progress run before that
        confirmation: disabled rows with a surviving PTY remain in the
        reconcile set and receive another official reap attempt next tick.
        """
        name = session_name(f"schedule-{schedule_id}")
        if self._is_abandoned(schedule_id):
            return False
        if (
            expected_generation is not None
            and get_shell_backend().session_generation(name) != expected_generation
        ):
            return False
        try:
            reaped, mode = get_shell_backend().kill_session(name)
        except Exception as exc:
            self._log_reap_failure(schedule_id, name, "raised", exc=exc)
            self._note_blocked(schedule_id, f"the session {name} could not be reaped")
            return False
        if not reaped:
            # SessionBackend's false verdict means the session survived its
            # own identity-aware kill. Keep metadata truthful until the next
            # bounded retry of the same official path.
            self._set_status(schedule_id, "running")
            self._log_reap_failure(schedule_id, name, "survived")
            self._note_blocked(schedule_id, f"the session {name} survived its reap")
            return False
        self._reap_retries.discard(schedule_id)
        self._reap_failure_logs.pop(schedule_id, None)
        self._blocked.pop(schedule_id, None)
        self._set_status(schedule_id, "stopped")
        # The killed process can never close its own run rows (the kill chain
        # is SIGKILL — no handler runs); close them now so a stop/restart/
        # edited-script save does not leave an in-progress "…" row behind (QA
        # P2-2). A kill that does not route through here leaves its row open
        # until the manager next launches or reaps this schedule.
        self._close_null_runs(schedule_id)
        _log.info("schedule %s: PTY session %s reaped (%s)", schedule_id, name, mode)
        return True

    # ── DB ──────────────────────────────────────────────────────────────────

    def _load_enabled(self) -> dict[int, str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, status FROM schedules WHERE enabled").fetchall()
        return {row[0]: row[1] for row in rows}

    def _claim_launch(self, schedule_id: int, revision: int) -> bool:
        """Admit one process attempt before effects; ambiguity consumes the attempt.

        Existing launch_count is a one-attempt latch, reset only by a new
        explicit revision. Old retry timestamps are retained but never read.
        """
        with write_transaction(self._pool) as conn:
            return (
                conn.execute(
                    "UPDATE schedules SET launch_count = 1, applied_revision = %s, "
                    "status = 'error', last_error = %s WHERE id = %s AND enabled "
                    "AND desired_revision = %s AND launch_count = 0",
                    (
                        revision,
                        "Launch outcome unknown; inspect before explicitly restarting",
                        schedule_id,
                        revision,
                    ),
                ).rowcount
                == 1
            )

    def _set_status(self, schedule_id: int, status: str) -> bool:
        with write_transaction(self._pool) as conn:
            return (
                conn.execute(
                    "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s",
                    (status, schedule_id),
                ).rowcount
                > 0
            )
