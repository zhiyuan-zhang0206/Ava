"""Gateway daemon that reminds agents about persistently idle shell sessions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, cast

import psycopg
from langchain_core.messages import AIMessage, HumanMessage
from psycopg_pool import ConnectionPool

import shared.checkpoint
import shared.db
import shared.pty_sessions.cli
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.lm._call import extract_text
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path, run_dir

from .engine import (
    IdleObservation,
    OwnerReminder,
    SessionState,
    advance,
    record_reminder,
)
from .state import load_state, save_state

_log = logging.getLogger("services.idle_shell_reminder.daemon")

_PIDFILE = settings.services.idle_shell_reminder_pidfile
_TICK_S = 60.0
_LIVENESS_TIMEOUT_S = 90.0
_LIVENESS_BEAT_STEP_S = 30.0
_SESSION_RE = re.compile(r"ava-agent-(\d+)-shell-(\d+)(?:-([a-z][a-z0-9-]*))?")


def _state_path() -> Path:
    return run_dir() / "idle_shell_reminder.json"


def _parse_session_name(name: str) -> tuple[int, int] | None:
    """Return ``(owner agent id, SDK shell id)`` for an owned shell name."""
    match = _SESSION_RE.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _is_page_session(name: str) -> bool:
    """Whether a shell name carries the daemon-owned ``page-`` suffix."""
    match = _SESSION_RE.fullmatch(name)
    return match is not None and (match.group(3) or "").startswith("page-")


def _retained_reply_ids(owner: int, inbound_ids: frozenset[int]) -> set[int]:
    """Return the latest reminder inbound id followed by an AI ``保留`` reply.

    ``ava_inbound_id`` is the checkpoint message contract used by
    ``agent.messages.inbound_message``. Only the latest candidate anchor is
    considered, matching an owner's reply to the reminder it could most
    recently have seen.
    """
    try:
        messages = shared.checkpoint.load_checkpoint_messages(owner)
    except shared.checkpoint.CheckpointReadError as exc:
        _log.debug("checkpoint unreadable for shell-reminder owner %s: %r", owner, exc)
        return set()

    anchor_index: int | None = None
    anchor_id: int | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        kwargs = cast("dict[str, object]", cast(Any, message).additional_kwargs)
        raw_id = kwargs.get("ava_inbound_id")
        if isinstance(raw_id, int) and raw_id in inbound_ids:
            anchor_index = index
            anchor_id = raw_id
    if anchor_index is None or anchor_id is None:
        return set()
    for message in messages[anchor_index + 1 :]:
        if isinstance(message, AIMessage) and "保留" in extract_text(message):
            return {anchor_id}
    return set()


def _idle_start_from_response(
    response: dict[str, Any], now_epoch: float, now_monotonic: float
) -> float | None:
    if response["ok"] is not True:
        raise ValueError(f"host returned error response {response!r}")
    fact = cast("dict[str, Any]", response["data"])
    if fact["idle"] is False:
        return None
    if fact["idle"] is not True:
        raise ValueError(f"non-boolean idle flag {fact['idle']!r}")
    idle_since = float(fact["idle_since"])
    if not math.isfinite(idle_since):
        raise ValueError(f"non-finite idle_since {idle_since!r}")
    return now_epoch - (now_monotonic - idle_since)


def _observations() -> tuple[tuple[IdleObservation, ...], set[str]]:
    """Poll live local PTY hosts and convert their monotonic idle clocks."""
    observations: list[IdleObservation] = []
    live_names: set[str] = set()
    now_epoch = time.time()
    now_monotonic = time.monotonic()
    for name in shared.pty_sessions.cli.live_sessions():
        parsed = _parse_session_name(name)
        if parsed is None:
            _log.debug("skipping unrecognized pty session name: %s", name)
            continue
        if _is_page_session(name):
            continue
        owner, sdk_id = parsed
        live_names.add(name)
        try:
            response = shared.pty_sessions.cli.session_request(name, {"op": "is_idle"})
        except OSError as exc:
            # The record still says live. Preserve existing state and retry next
            # tick instead of treating an unresponsive host as session death.
            _log.debug("idle poll failed for pty session %s: %r", name, exc)
            continue
        try:
            idle_start = _idle_start_from_response(response, now_epoch, now_monotonic)
        except (KeyError, TypeError, ValueError) as exc:
            _log.debug("invalid idle response for pty session %s: %r", name, exc)
            continue
        observations.append(
            IdleObservation(name=name, owner=owner, sdk_id=sdk_id, idle_start=idle_start)
        )
    return tuple(observations), live_names


def _alive_owners(pool: ConnectionPool, owners: set[int]) -> set[int]:
    """Owners whose metadata row is not explicitly terminated."""
    if not owners:
        return set()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta WHERE id = ANY(%s) AND status != 'terminated'",
            (sorted(owners),),
        )
        return {int(row[0]) for row in cur.fetchall()}


def _deliver_reminders(
    pool: ConnectionPool,
    state: dict[str, SessionState],
    reminders: tuple[OwnerReminder, ...],
    now: float,
) -> None:
    for reminder in reminders:
        try:
            with pool.connection() as conn:
                inbound_id = shared.db.insert_inbound_message(
                    conn,
                    reminder.owner,
                    reminder.content,
                    source="system",
                    kind="chat",
                )
        except psycopg.ProgrammingError:
            raise
        except Exception:
            _log.exception("idle-shell reminder delivery failed for owner %s", reminder.owner)
            continue
        record_reminder(
            state,
            reminder,
            inbound_id=inbound_id,
            reminded_at=now,
        )
        _log.info(
            "sent idle-shell reminder inbound %s to owner %s for %s session(s)",
            inbound_id,
            reminder.owner,
            len(reminder.sessions),
        )


def _tick(
    pool: ConnectionPool,
    state: dict[str, SessionState],
    path: Path,
) -> dict[str, SessionState]:
    observations, live_names = _observations()
    owners = {observation.owner for observation in observations}
    alive_owners = _alive_owners(pool, owners)
    now = time.time()
    next_state, reminders = advance(
        state,
        now=now,
        observations=observations,
        live_session_names=live_names,
        owner_alive=alive_owners.__contains__,
        retained_reply_ids=_retained_reply_ids,
    )
    _deliver_reminders(pool, next_state, reminders, now)
    try:
        save_state(path, next_state)
    except Exception:
        # Keep the in-memory state so a transient filesystem failure does not
        # duplicate reminders on every tick. A restart may repeat the last
        # reminder, which is preferable to killing the daemon's work loop.
        _log.exception("could not persist idle-shell reminder state to %s", path)
    return next_state


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.idle_shell_reminder.daemon"):
        _log.info("idle-shell-reminder already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    return pidfile_holds_daemon(
        _PIDFILE, "services.idle_shell_reminder.daemon"
    ) or pidfile_holds_daemon(
        legacy_pid_path("idle_shell_reminder"), "services.idle_shell_reminder.daemon"
    )


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _scan_loop(
    pool: ConnectionPool,
    liveness: Liveness,
    state: dict[str, SessionState],
    path: Path,
) -> None:
    _log.info("idle-shell-reminder started, pid=%s, interval=%.0fs", os.getpid(), _TICK_S)
    while True:
        try:
            state = _tick(pool, state, path)
            liveness.beat()
            await _sleep_with_liveness(liveness, _TICK_S)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "idle-shell-reminder schema / syntax error; daemon exiting",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("idle-shell-reminder tick failed")
            await _sleep_with_liveness(liveness, _TICK_S)


async def run() -> None:
    if _is_running():
        _log.info("idle-shell-reminder already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("idle-shell-reminder pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("idle_shell_reminder", liveness=liveness)
    _log.info("idle-shell-reminder healthz listening on :%s", health_port("idle_shell_reminder"))
    pool = shared.db.pool()
    path = _state_path()
    state = load_state(path)
    try:
        await _scan_loop(pool, liveness, state, path)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("idle-shell-reminder stopped")


def main() -> None:
    from shared.migrations import assert_schema_current

    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="idle_shell_reminder")
    install_graceful_shutdown("idle_shell_reminder")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("idle-shell-reminder interrupted, shutting down")
    except Exception:
        _log.exception("idle-shell-reminder crashed")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
