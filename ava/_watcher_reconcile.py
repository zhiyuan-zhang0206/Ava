"""Watcher desired-state reconciliation at agent boot."""

from __future__ import annotations

import contextlib
import datetime
import logging
import pathlib
from collections.abc import Callable
from typing import Any

from shared.watcher import TEMPLATE_VERSION

_agent_id: Callable[[], int]
_watchers_dir: Callable[[], pathlib.Path]
cron: Callable[..., int]
at: Callable[..., int]
launch: Callable[..., int]
logger: logging.Logger


def bind(
    *,
    agent_id: Callable[[], int],
    watchers_dir: Callable[[], pathlib.Path],
    cron_fn: Callable[..., int],
    at_fn: Callable[..., int],
    launch_fn: Callable[..., int],
    target_logger: logging.Logger,
) -> None:
    """Bind watcher facade dependencies without a reverse module import."""
    global _agent_id, _watchers_dir, cron, at, launch, logger  # noqa: PLW0603
    _agent_id = agent_id
    _watchers_dir = watchers_dir
    cron = cron_fn
    at = at_fn
    launch = launch_fn
    logger = target_logger


def _kill_watcher_orphan_processes(session_id: int) -> None:
    """SIGKILL any live process still running this watcher's generated script.

    A watcher whose session is gone but whose process is alive is an orphan:
    its pty host died (crash / SIGKILL / a reaper sweep) and the child was
    reparented to init — still alive, still firing cron/at (task #1726:
    49 of 85 watcher processes on the fleet host were multi-generation
    orphans of exactly this shape). The boot reconcile rebuilds the schedule
    from the registry; without this kill, each rebuild stacks a NEW
    generation on the orphan and both fire. The generated boot script path is
    unique per (agent, session), so matching it in the process's argv is
    precise; SIGKILL matches the pty reaper's convention for detached orphans
    (shared/pty_sessions/orphan_reaper.py) and skips the child's finally, so
    its registry row survives for the transition below. Fail-soft: a scan
    error must not block the boot reconcile it serves.
    """
    import psutil

    boot_path = str(_watchers_dir() / f"watcher_{session_id}_boot.py")
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if boot_path in (proc.info.get("cmdline") or []):
                proc.kill()
        except (psutil.Error, OSError):
            continue


def _live_cron_session(
    agent_id: int,
    *,
    cron_expr: str,
    cron_timezone: str,
    cron_end_at: datetime.datetime | None,
    generation: str | None,
    alive: set[int] | None,
    exclude_session: int | None = None,
) -> int | None:
    """Return the session id of an existing LIVE cron watcher with the same
    schedule (Task #1825), or None.

    Two watchers with the same (agent, kind, schedule) — expression,
    timezone, end time — are duplicates: they fire the same wake-ups, and a
    kill/rebuild cycle that stacked them (a killed cron resurrected as a new
    session while the old registration survived) made both fire concurrently
    (observed twice: #2811's triple instance, CEO #228's escalation-check-6h
    sessions 52+53). The reconcile and cron() dedupe through this: a live
    duplicate is reused instead of stacking a new generation.

    "Live" means the registry row is still `running` AND its session is in
    the caller's session list. A row whose session is gone is exactly what
    the reconcile is rebuilding — it is not a duplicate to reuse. `alive=None`
    (session list unavailable) deliberately finds nothing: returning a session
    that might be dead would silently lose the schedule, and a duplicate is
    recoverable while a lost wake-up is not. `exclude_session` skips one live
    row — the stale-template rebuild must not dedupe against the very session
    it is replacing.

    Fail-soft: a registry read failure logs and yields no duplicate — the
    dedupe is advisory and must never block a registration.
    """
    from shared.watcher_registry import watcher_rows

    if alive is None:
        return None
    try:
        rows = watcher_rows(agent_id)
    except Exception:
        logger.warning(
            "watcher dedupe: registry read failed for agent %s — proceeding without dedupe",
            agent_id,
            exc_info=True,
        )
        return None
    for row in rows:
        if row["kind"] != "cron" or row["status"] != "running":
            continue
        if row.get("generation") != generation:
            continue
        if row["session_id"] == exclude_session:
            continue
        if row["session_id"] not in alive:
            continue
        if (
            row["cron_expr"] == cron_expr
            and row["cron_timezone"] == cron_timezone
            and row["cron_end_at"] == cron_end_at
        ):
            return row["session_id"]
    return None


def _notify_missed_watcher(agent_id: int, content: str) -> None:
    """Best-effort notification for a one-shot watcher that cannot run."""
    from ava import agents as _agents

    with contextlib.suppress(Exception):
        _agents.send_message(agent_id, content)


def _reconcile_missing(
    row: dict[str, Any],
    now: datetime.datetime,
    generation: str | None,
    alive: set[int],
) -> str | None:
    """Handle one registry row whose session is gone: rebuild a standing
    schedule / future one-shot, mark a passed one-shot or launch watcher missed,
    drop an ended schedule. Returns the action sentence (None when nothing was
    done — an ended cron schedule deletes its row without an action).

    `alive` is the caller's live-session set, used to spot a live duplicate
    before rebuilding a cron (Task #1825)."""
    from shared.watcher_registry import delete_watcher, mark_status, wake_delivered

    agent_id = _agent_id()
    session_id = row["session_id"]
    name = row["name"]
    try:
        # The session is gone by definition here — any live process still
        # running this watcher's script is an orphan of a dead host, still
        # firing. Kill it before the rebuild/mark below: a rebuild that does
        # not kill the orphan stacks a new generation on it and both fire
        # (task #1726). No-op when no such process exists.
        _kill_watcher_orphan_processes(session_id)
        if row["kind"] == "cron":
            end_at = row["cron_end_at"]
            if end_at is not None and end_at < now:
                delete_watcher(agent_id, session_id)
                return f"cron watcher '{name}': schedule ended; row dropped"
            # Dedupe (Task #1825): the schedule may already be live under
            # another session (a duplicate registration survived a
            # kill/restart cycle — #2811, CEO #228). Rebuild would stack a
            # second generation on it and both would fire; reuse it instead
            # and drop this dead duplicate row.
            existing = _live_cron_session(
                agent_id,
                cron_expr=row["cron_expr"],
                cron_timezone=row["cron_timezone"],
                cron_end_at=end_at,
                generation=generation,
                alive=alive,
                exclude_session=session_id,
            )
            if existing is not None:
                delete_watcher(agent_id, session_id)
                return (
                    f"cron watcher '{name}': duplicate of live session {existing}; dead row dropped"
                )
            new_id = cron(
                row["cron_expr"],
                row["message"] or "",
                timezone=row["cron_timezone"],
                end_time=row["cron_end_at"],
                name=name,
            )
            mark_status(agent_id, session_id, "rebuilt")
            return f"cron watcher '{name}' rebuilt as session {new_id}"
        if row["kind"] == "at":
            if row["fires_at"] is not None and row["fires_at"] > now:
                new_id = at(row["fires_at"], row["message"] or "", name=name)
                mark_status(agent_id, session_id, "rebuilt")
                return f"one-shot watcher '{name}' rebuilt as session {new_id}"
            # Delivery check (task #1858): the child deletes its own row on a
            # clean exit, but that delete can fail or race the reconcile, and
            # a one-shot that already fired and delivered its wake must not be
            # reported as missed (observed 2026-08-27: the wake arrived at
            # 18:30:00 and the same session was alerted "marked missed" two
            # seconds later). A delivered wake means the row is stale, not a
            # lost moment: drop it silently.
            if wake_delivered(agent_id, session_id, row["message"] or "", row["created_at"]):
                delete_watcher(agent_id, session_id)
                return f"one-shot watcher '{name}' already fired; row dropped"
            mark_status(agent_id, session_id, "missed")
            _notify_missed_watcher(
                agent_id,
                f"[watcher] '{name}' was not running at boot and its "
                f"moment ({row['fires_at']}) has passed — marked missed.",
            )
            return f"one-shot watcher '{name}' marked missed"
        mark_status(agent_id, session_id, "missed")
        _notify_missed_watcher(
            agent_id,
            f"[watcher] '{name}' (one-shot launch watcher) was not "
            "running at boot — marked missed.",
        )
        return f"launch watcher '{name}' marked missed"
    except Exception:
        logger.warning(
            "watcher reconcile: failed for session %s (%s)",
            session_id,
            name,
            exc_info=True,
        )
        return None


def _reap_superseded_watcher(row: dict[str, Any], alive: set[int]) -> str:
    """Retain an obsolete desired record while reaping its exact session.

    A row from another allocation generation must never reach the ordinary
    missing-session path: its cron payload was desired by the previous
    generation, not a declaration to recreate in this one.  The retained
    `reaped` status makes that decision auditable without reintroducing it on
    a later boot.
    """
    from ava.shell import sessions as _sessions_mod
    from shared.watcher_registry import mark_status

    session_id = row["session_id"]
    if session_id in alive:
        if not _sessions_mod._reap(session_id):
            logger.warning(
                "watcher reconcile: could not reap superseded session %s (%s)",
                session_id,
                row["name"],
            )
            return f"watcher '{row['name']}' from superseded generation retained for reaping"
        alive.discard(session_id)
    _kill_watcher_orphan_processes(session_id)
    agent_id = _agent_id()
    mark_status(agent_id, session_id, "reaped")
    if row["kind"] in ("at", "launch"):
        _notify_missed_watcher(
            agent_id,
            f"[watcher] '{row['name']}' ({row['kind']} one-shot watcher) was reaped "
            "by an allocation generation flip before it could run — marked missed.",
        )
    return f"watcher '{row['name']}' from superseded generation reaped"


def _rebuild_stale_cron_watcher(row: dict[str, Any]) -> str | None:
    """Rebuild one live cron watcher whose spawn template version is behind the
    current template; return the action sentence (None when the rebuild failed).

    The generated script is frozen at launch, so a template fix (issue #182)
    never reaches a running session — it keeps the old loop (and the double-fire
    at a stepped boundary, issue #1330) until rebuilt. The replacement is
    spawned first (its own registry row carries the rebuild duty from here),
    then the stale session is killed (dropping its old row — the
    deliberate-kill semantics).
    """
    from ava.shell import sessions as _sessions_mod

    session_id = row["session_id"]
    name = row["name"]
    try:
        new_id = cron(
            row["cron_expr"],
            row["message"] or "",
            timezone=row["cron_timezone"],
            end_time=row["cron_end_at"],
            name=name,
            # The session being replaced is LIVE (this is a template upgrade,
            # not a death recovery) — the dedupe must not reuse it, or the
            # rebuild would kill the only live copy and leave nothing.
            _exclude_session=session_id,
        )
        _sessions_mod.kill(session_id)
        return (
            f"cron watcher '{name}' rebuilt as session {new_id} "
            f"(stale template v{row.get('template_version') or 0} "
            f"-> v{TEMPLATE_VERSION})"
        )
    except Exception:
        logger.warning(
            "watcher reconcile: stale cron watcher '%s' (session %s) rebuild failed",
            name,
            session_id,
            exc_info=True,
        )
        return None


def reconcile() -> list[str]:
    """Rebuild / mark watchers whose sessions died — the #1014 fix (R1-8).

    Called from host runtime preparation (`agent/_process_boot.py`): every row in this agent's
    watcher registry whose session is gone is either rebuilt or marked:

    - `cron` — re-spawned from the stored expression only when its row belongs
      to the current allocation generation (the standing schedule is
      the whole point of the registry: a rollout reaped its session and nothing
      else knew it should exist). A row from a superseded generation is reaped
      and retained as history instead. A schedule whose `end_time` has passed is
      deleted instead — it ended, just not cleanly. The old row is marked
      `rebuilt`; the new session gets its own `running` row.
    - `at` — re-spawned while its moment is still in the future; once the
      moment has passed the wake is lost, so the row is marked `missed` and the
      agent is told (it created the one-shot; it should know it never fired).
      A passed one-shot whose wake WAS delivered (its child fired, but the
      clean-exit row delete failed or raced) is dropped silently instead — the
      moment was not lost, so no alert (task #1858).
    - `launch` — one-shot scripts are not re-run at boot (their work is
      time-bound and probably stale); marked `missed` + alerted.

    A row whose session is alive is left alone — except a live cron watcher
    whose spawn template version is behind the current one, which is rebuilt so
    a template fix reaches sessions that were already running when it landed
    (issue #1330). Fail-soft: a registry or spawn failure is logged and
    skipped, never allowed to block the boot it runs in.

    Returns the action sentences (empty when nothing needed doing).
    """

    from ava.shell import sessions as _sessions_mod
    from shared.watcher_registry import (
        watcher_rows,
    )

    agent_id = _agent_id()
    try:
        rows = watcher_rows(agent_id)
    except Exception:
        logger.warning("watcher reconcile: registry read failed", exc_info=True)
        return []
    if not rows:
        return []
    try:
        alive = set(_sessions_mod.list())
    except Exception:
        logger.warning("watcher reconcile: session list failed", exc_info=True)
        return []

    actions: list[str] = []
    now = datetime.datetime.now(datetime.UTC)
    current_generation = _sessions_mod._current_session_generation()
    for row in rows:
        if row["status"] != "running":
            # rebuilt / missed are terminal history: a rebuilt row's live
            # replacement is its own new running row (which carries the
            # rebuild duty from here), and a missed one-shot was already
            # marked + alerted. Re-processing them would re-spawn a duplicate
            # on every boot — observed 2026-08-09: after one rollout each
            # cron came back TWICE (rebuilt rows kept re-rebuilding).
            continue
        session_id = row["session_id"]
        if row.get("generation") != current_generation:
            actions.append(_reap_superseded_watcher(row, alive))
            continue
        if session_id in alive:
            # Session liveness alone cannot make an exact record current: a
            # stale host can retain the old name across a flip. Reap that
            # record, then let the still-current desired row rebuild below.
            if _sessions_mod._session_generation(session_id) != current_generation:
                if not _sessions_mod._reap(session_id):
                    logger.warning(
                        "watcher reconcile: could not reap stale session %s (%s)",
                        session_id,
                        row["name"],
                    )
                    continue
                alive.discard(session_id)
            else:
                # Live but stale: a generated watcher script is frozen at launch, so
                # a template fix (issue #182 / #1330) never reaches a session that
                # was already running when it landed — it keeps the old loop until
                # rebuilt. Rebuild live cron watchers whose spawn version is behind
                # the current template: spawn the replacement first (its own row
                # carries the rebuild duty from here), then kill the stale session
                # (which drops its old row — the deliberate-kill semantics).
                if row["kind"] == "cron" and (row.get("template_version") or 0) < TEMPLATE_VERSION:
                    action = _rebuild_stale_cron_watcher(row)
                    if action:
                        actions.append(action)
                continue
        action = _reconcile_missing(row, now, current_generation, alive)
        if action:
            actions.append(action)
    return actions
