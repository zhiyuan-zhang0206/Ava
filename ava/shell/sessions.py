"""Persistent shell sessions that preserve cwd, env, and background processes across calls."""

__all_for_ava__ = ["capture", "kill", "list", "new", "renew", "send", "send_keys"]

import builtins
import contextlib
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import ava
import ava.agent_identity
from ava._sdk_validation import coerce_str, coerce_typed
from ava.security import scan_content
from shared.cluster import session_name
from shared.config import settings
from shared.paths import repo_root, workspace_dir
from shared.session_backend import get_shell_backend
from shared.session_env import cwd_is_inside_checkout, forward_env_dict

logger = logging.getLogger(__name__)


def _agent_prefix() -> str:
    # Generic prefix for all shell sessions of the current agent:
    # `ava-agent-<agent_id>-`. The agent's process, its shells, and
    # its watchers all share this prefix; kill_all filters on it.
    return f"{session_name(f'agent-{ava.agent_identity.agent_id()}')}-"


def _shell_prefix() -> str:
    # Base prefix for every session this agent owns. All sessions share the
    # `shell-<session_id>` base; a named session carries an extra `-<name>`
    # suffix on top (watchers use the conventional name "watcher").
    # `_shell_prefix()` therefore matches both.
    return f"{_agent_prefix()}shell-"


def _next_session_index_from_db() -> int:
    # Atomically increment agents_meta.session_index to fetch the next session
    # number (shared by shells and watchers). Uses `UPDATE ... RETURNING` for
    # concurrency safety. No fallback — raise directly if DB is unavailable or
    # the agent isn't in agents_meta.
    import psycopg

    from ava._settings import DB_URL
    from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

    agent_id = ava.agent_identity.agent_id()
    if agent_id is None:  # type: ignore[unnecessary-isinstance]  # agent_identity.agent_id() returns None pre-bootstrap (type annotation is int for call-site simplicity)
        raise RuntimeError(
            "Cannot allocate a session index: this process has no agent identity. "
            "ava.shell.sessions.new() requires an agent process or a background "
            "script launched by one (which receives the identity via "
            "ava.agent_identity.establish). Running a standalone script that imports ava "
            "does not set an agent identity."
        )
    # PG_KEEPALIVE_KWARGS: this runs inside the agent's exec sandbox, so a
    # black-holing database would otherwise hang `ava.shell.new()` on the OS
    # TCP-retransmit timeout instead of raising. Same constant as shared.db.
    # prepare_threshold=None: never prepare statements on the pooled front door.
    with (
        psycopg.connect(DB_URL, prepare_threshold=None, **PG_STATEMENT_TIMEOUT_KWARGS) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SET TRANSACTION READ WRITE")
        cur.execute(
            "UPDATE agents_meta SET session_index = session_index + 1 "
            "WHERE id = %s RETURNING session_index",
            (agent_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError(
            f"agent {agent_id} not in agents_meta table — cannot allocate session_index"
        )
    # session_index DEFAULT 0; first UPDATE RETURNING 1 → returns 0
    return int(row[0]) - 1


def _list_all_sessions() -> builtins.list[str]:
    # List all sessions on the shell backend (no prefix filtering). The PTY
    # backend answers only its own sessions, which are exactly this agent's
    # shells + watchers since the session-backend migration (services live on
    # get_backend(), orchestration on the session backend) — the same filtering that follows needs no more.
    return get_shell_backend().list_sessions()


def _own_sessions() -> builtins.list[str]:
    # Full session names of this agent's sessions (shells + watchers).
    prefix = _shell_prefix()
    return [s for s in _list_all_sessions() if s.startswith(prefix)]


def _session_generation(session_id: int) -> str | None:
    """Persisted generation of one live shell session, if the backend tracks it."""
    try:
        target = _resolve(session_id)
    except ValueError:
        return None
    return get_shell_backend().session_generation(target)


def _current_session_generation() -> str | None:
    """Host flip generation used to classify desired session records."""
    from shared.sessions.pty.allocation_freeze import current_generation

    return current_generation()


def _reap(session_id: int) -> bool:
    """Reap one exact session without changing its desired-state record.

    Desired-state reconcilers use this for a superseded generation, then retain
    their own record as terminal history. Public ``kill()`` intentionally has
    different semantics: a user cancellation deletes any watcher registry row
    so it cannot be restored.
    """
    try:
        target = _resolve(session_id)
    except ValueError:
        return False
    ok, _mode = get_shell_backend().kill_session(target, graceful=False)
    return ok


def _resolve(session_id: int) -> str:
    # Resolve an int session id to its full session name. The id is unique
    # within the agent, so `…-shell-<id>` matches exactly one session whether
    # or not it carries a `-<name>` suffix. Prefix-matching `<base>-` cannot
    # conflate ids: `…-shell-12` does not start with `…-shell-1-`. Raise if the
    # id does not belong to this agent.
    base = f"{_shell_prefix()}{session_id}"
    for name in _own_sessions():
        if name == base or name.startswith(f"{base}-"):
            return name
    raise ValueError(f"session {session_id} is not this agent's (no match for {base!r})")


# A session name is a lowercase slug starting with a letter: it rides inside the
# session identifier (after the numeric id), so the charset stays conservative
# and the leading letter keeps it visually distinct from the id it follows.
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")


_MAX_TTL_SECONDS = 86_400  # 24h — user-facing sessions only; watchers exempt (see _validate_ttl)


def _validate_ttl(ttl: float, *, system: bool = False) -> float:
    """Validate a TTL. The 24h cap protects user calls only.

    `system=True` marks a system-side session (a watcher spawn): its ttl is
    the watcher's true target deadline (user ruling 2026-09-14, task #3411)
    and is exempt from the sessions.new cap of 2026-09-01 — a 7-day standing
    cron is a normal watcher, not a user session. A non-finite or
    non-positive ttl is refused on every path: a session whose deadline has
    already passed must never be created (the reconcile's no-rebuild guard).
    """
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl must be finite and greater than zero")
    if not system and ttl > _MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl must be at most {_MAX_TTL_SECONDS} seconds (24 hours) — "
            "sessions live at most one day"
        )
    return ttl


def _record_ttl(session_id: int, ttl: float) -> None:
    """Write the session's mandatory deadline row to `agent_shell_ttls`.

    The gateway TTL reaper reads this table (the runner role holds INSERT).
    Fail-loud: without the row the reaper can never reclaim the session, so
    the caller must abort the creation it just made. `SET TRANSACTION READ
    WRITE` leads the transaction — a pooled backend handed over with
    session-level read-only poison would otherwise reject the write."""
    import psycopg

    from ava._settings import DB_URL
    from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

    try:
        with (
            psycopg.connect(DB_URL, prepare_threshold=None, **PG_STATEMENT_TIMEOUT_KWARGS) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() + make_interval(secs => %s))",
                (ava.agent_identity.agent_id(), session_id, ttl),
            )
            conn.commit()
    except Exception as exc:
        raise RuntimeError(f"failed to track TTL for session {session_id}") from exc


def _create_session(
    name: str | None = None,
    *,
    cwd: str | None = None,
    ttl: float,
    system: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, str]:
    # Allocate the next session id and create the shell session. `name` becomes
    # a `-<name>` suffix on the session identifier (None = unnamed). `cwd` sets
    # the session's starting directory (None = the agent's workspace, the same
    # base ava.shell.run uses). Returns (id, full_name). Internal — `new()`
    # (shell), `run_background`, and `ava.watcher._spawn` (named "watcher")
    # all use it.
    #
    # `ttl` is REQUIRED: user ruling 2026-08-27 made shell TTL mandatory (the
    # idle-shell-reminder daemon is gone and TTL is the only reclamation
    # mechanism). Every caller — shells and watchers alike — records its
    # deadline in `agent_shell_ttls`. For a watcher that deadline IS the
    # watcher's target (launch timeout / cron end / at moment + grace; user
    # ruling 2026-09-14, task #3411) — one lifecycle system, not a registry
    # lifetime beside a decorative shell TTL.
    if name is not None and not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"session name {name!r} invalid — use a lowercase slug like 'dev-server' "
            "([a-z][a-z0-9-]*)"
        )
    # Validate here, not at call sites, so every caller is capped at the
    # write point (ruling 2026-09-01: sessions live at most 24h) — except
    # system-side callers (watcher spawns), whose ttl IS the watcher's true
    # target deadline and is exempt (ruling 2026-09-14, task #3411).
    ttl = _validate_ttl(ttl, system=system)
    session_id = _next_session_index_from_db()
    full = f"{_shell_prefix()}{session_id}" + (f"-{name}" if name is not None else "")
    # Pass the session allowlist to the backend. On POSIX the detached PTY host
    # also inherits the launcher's ambient env before overlaying this dict;
    # watcher-only runner credentials arrive through `env_overrides`, while
    # generic shell sessions keep the ordinary projection.
    #
    # The override rides the backend's 0600 envfile, not argv (issue #974).
    backend = get_shell_backend()
    if cwd is None:
        agent_id = ava.agent_identity.agent_id()
        # agent id is typed int but is None until a bootstrap establishes it
        # (same fallback as ava.shell.run — the DB call above already raised
        # pre-bootstrap, so this is only about resolving the base).
        cwd = str(workspace_dir(agent_id)) if agent_id is not None else str(Path.home())  # pyright: ignore[reportUnnecessaryComparison]
    # The id is allocated before the host-level PTY admission gate. During an
    # operator freeze a refused attempt therefore leaves a harmless gap in this
    # monotonic per-agent sequence. Never roll it back or reuse it: an old
    # numeric handle must remain stale instead of naming a later session.
    session_cwd = Path(cwd)
    activate_venv = cwd_is_inside_checkout(session_cwd, repo_root())
    session_env = forward_env_dict(activate_venv=activate_venv)
    if env_overrides:
        session_env.update(env_overrides)
    ok = backend.new_session(
        full,
        "",
        session_cwd,
        env=session_env,
    )
    if not ok:
        raise RuntimeError(f"failed to create session {full!r}")
    try:
        _record_ttl(session_id, ttl)
    except RuntimeError:
        # An untracked session could never be reclaimed — dispose of it
        # rather than leaving a live session the reaper cannot see.
        with contextlib.suppress(Exception):
            backend.kill_session(full, graceful=False)
        raise
    return session_id, full


def new(name: str, *, ttl: float) -> int:
    """`name` is only a display label — a lowercase slug like `"dev-server"`;
    every operation takes the id.

    Args:
        ttl: same semantics as `run_background` — required hard lifetime in
            seconds from creation; a deadline-bound task belongs in
            `ava.watcher` instead. Extend the deadline later, before it
            passes, with `renew(id, ttl=)`."""
    name = coerce_str(name, "name")
    ttl = coerce_typed(ttl, "ttl", (int, float))
    session_id, _ = _create_session(name, ttl=ttl)
    return session_id


# `id` / `list` shadow builtins intentionally: these are the agent-facing names.
# (flake8-builtins `A` is not in this repo's ruff select, so no noqa is needed.)
def send(id: int, cmd: str, *, enter: bool = True) -> None:
    """Asynchronous — returns immediately without waiting for the command.

    Set `enter=False` to type the string without pressing Enter.

    Long text (over roughly 1K characters) sent into a terminal UI can be
    folded by its input handling and arrive silently incomplete (Claude Code
    drops the folded part on a mixed submission; task #4364). Wrap such text
    as a bracketed paste, or send a short pointer to a file the receiver
    reads."""
    id = coerce_typed(id, "id", int)
    cmd = coerce_str(cmd, "cmd")
    enter = coerce_typed(enter, "enter", bool)
    # Text and Enter go in separate writes: a combined write races TUI
    # programs (Claude Code, Codex) that are still processing the typed text
    # when Enter arrives.
    backend = get_shell_backend()
    target = _resolve(id)
    backend.send(target, cmd)
    if enter:
        backend.send_keys(target, "Enter")


def send_keys(id: int, *keys: str) -> None:
    """Send raw keys to a session without submitting a line. Each argument is
    one key: a single character, or a name like `C-c`, `Escape`, `Up`,
    `Enter`, `Space`, `PageUp`."""
    id = coerce_typed(id, "id", int)
    keys = tuple(coerce_str(key, "key") for key in keys)
    get_shell_backend().send_keys(_resolve(id), *keys)


def capture(id: int, lines: int | None = None, *, scrollback: bool = True) -> str:
    """The session's most recent `lines` of output, including history that
    has scrolled past. Omit `lines` for the configured default
    (``display.shell_capture_default_lines``, 200 out of the box). Pass
    `scrollback=False` to get only the current visible screen instead —
    needed for full-screen programs that redraw in place (`lines` is
    ignored then)."""
    id = coerce_typed(id, "id", int)
    resolved_lines: int
    if lines is None:
        resolved_lines = settings.display.shell_capture_default_lines
    else:
        resolved_lines = coerce_typed(lines, "lines", int)
    scrollback = coerce_typed(scrollback, "scrollback", bool)
    # A session holds whatever ran in it — an interactive fetch, a coding agent
    # rendering a web page — so reading one ingests exactly as `shell.run` does.
    # Scanned for the same reason; the text comes back byte-for-byte.
    name = _resolve(id)
    pane = get_shell_backend().capture_pane(name, resolved_lines, scrollback=scrollback)
    return scan_content(pane, source="shell.sessions.capture")


def kill(id: int) -> None:
    id = coerce_typed(id, "id", int)
    backend = get_shell_backend()
    ok, _mode = backend.kill_session(_resolve(id), graceful=False)
    if not ok:
        raise RuntimeError(f"failed to kill session {id}")
    # R1 (Task #1021): a deliberately killed watcher must not be rebuilt by the
    # next boot reconcile — drop its registry row. A non-watcher session has no
    # row, so this is a no-op there. Fail-soft: a registry blip must not make
    # the kill itself fail — but it must not be silent either: a surviving row
    # reads at the next boot reconcile as "killed, should exist" and rebuilds
    # the very watcher this kill ended (the same visibility discipline the boot
    # script's clean-exit delete follows).
    try:
        from ava import agent_identity
        from shared.daemon.schedules.watcher_registry import delete_watcher

        delete_watcher(int(agent_identity.agent_id()), id)
    except Exception:
        logger.warning(
            "watcher registry row delete failed after killing session %s — "
            "the next boot reconcile may rebuild the killed watcher",
            id,
            exc_info=True,
        )


# Not in __all_for_ava__, so never rendered into the SDK docs: a prefix-scoped
# cleanup helper used by test fixtures. The agent exit path deliberately does
# NOT reap sessions — they outlive the turn (see services/agent_host/host.py); the agent
# itself kills sessions one by one via kill(id).
def kill_all() -> int:
    sessions = _own_sessions()
    for name in sessions:
        with contextlib.suppress(RuntimeError):
            get_shell_backend().kill_session(name, graceful=False)
    # Same deliberate-kill semantics as kill(): every watcher this agent just
    # killed must not be resurrected by the next boot reconcile (Task #1825 —
    # a kill path that left the registry row behind made a killed cron come
    # back as a second live instance). Fail-soft: a registry blip must not
    # make the cleanup itself fail — visible for the same reason as kill().
    try:
        from ava import agent_identity
        from shared.daemon.schedules.watcher_registry import delete_watcher, watcher_session_ids

        agent_id = int(agent_identity.agent_id())
        for session_id in watcher_session_ids(agent_id=agent_id):
            delete_watcher(agent_id, session_id)
    except Exception:
        logger.warning(
            "watcher registry cleanup after kill_all failed — killed watchers "
            "may be rebuilt at the next boot",
            exc_info=True,
        )
    return len(sessions)


def renew(id: int, *, ttl: float) -> datetime:
    """Extend a live session's TTL deadline by `ttl` seconds.

    The deadline moves to now + ttl — never stacked on the current deadline.
    `ttl` is required, capped at 24h per call; renewal has no lifetime cap.

    Only this agent's live, not-yet-expired sessions can renew. Watcher
    sessions are refused too — their shell TTL is the target deadline, so
    re-register the schedule instead (`ava.watcher.cron` / `ava.watcher.at`).

    Returns:
        The new deadline (DB clock).
    """
    id = coerce_typed(id, "id", int)
    ttl = _validate_ttl(coerce_typed(ttl, "ttl", (int, float)))
    # Not this agent's / not alive -> ValueError, same rule as send/capture.
    _resolve(id)
    agent_id = int(ava.agent_identity.agent_id())
    from shared.daemon.schedules.watcher_registry import watcher_session_ids

    if id in watcher_session_ids(agent_id):
        raise ValueError(
            f"session {id} is a watcher — its TTL is derived from the watcher's "
            "target deadline (launch timeout / cron end / at moment), so renewal "
            "would desync the TTL from that target; extend the schedule by "
            "re-registering it (ava.watcher.cron/at), which re-mounts the session"
        )
    return _record_renewal(agent_id, id, ttl)


def _record_renewal(agent_id: int, session_id: int, ttl: float) -> datetime:
    """Extend the deadline row and write the audit trail.

    The write UPDATEs the deadline in one transaction guarded by
    ``expires_at > clock_timestamp()``: an expired row is never renewable.
    The guard is what makes the pair with the reaper airtight — a successful
    renewal proves the row was unexpired at its write, so no expired-row
    select could have returned it before that write, and a renewal attempted
    after the deadline loses (the reaper owns the session). ``SET TRANSACTION
    READ WRITE`` leads the transaction — same pooled read-only poison
    defense as `_record_ttl`.

    Audit trail: one `agent_shell_ttl_renewals` row (requested ttl +
    before/after deadlines) in the same transaction as the deadline write,
    plus the `renewals` counter and `last_renewed_at` on the main row, plus
    a telemetry `shell_ttl_renewed` event. The before/after read and the
    write use separate connections on purpose: the guarded UPDATE re-checks
    the deadline itself, so the read needs no lock, and a concurrent second
    renewal from a background process can at worst stamp a slightly stale
    `prev_expires_at` — deadlines stay monotone and this path is
    single-agent, single-turn by construction, not worth locking for.
    """
    prev_expires = _read_expiry_row(agent_id, session_id)
    if prev_expires is None:
        raise RuntimeError(
            f"session {session_id} has no TTL row — a pre-mandate session cannot be renewed"
        )
    new_expires = _apply_renewal(agent_id, session_id, ttl, prev_expires)
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "shell_ttl_renewed",
        level="info",
        agent_id=agent_id,
        attributes={
            "session_id": session_id,
            # int cast stabilizes the emitted metric kind (task #4011).
            "ttl_s": round(ttl),
            "prev_expires_at": prev_expires.isoformat(),
            "new_expires_at": new_expires.isoformat(),
        },
    )
    return new_expires


def _read_expiry_row(agent_id: int, session_id: int) -> datetime | None:
    """The session's current deadline; None when the row is absent."""
    import psycopg

    from ava._settings import DB_URL
    from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

    try:
        with (
            psycopg.connect(DB_URL, prepare_threshold=None, **PG_STATEMENT_TIMEOUT_KWARGS) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, session_id),
            )
            row = cur.fetchone()
    except psycopg.Error as exc:
        raise RuntimeError(f"failed to renew session {session_id}") from exc
    return row[0] if row is not None else None


def _renewal_update(cur: Any, agent_id: int, session_id: int, ttl: float) -> datetime | None:
    """The guarded deadline write; None when the row is no longer unexpired.

    The guard compares against ``clock_timestamp()`` — the statement time,
    evaluated after the row lock is acquired — never ``now()`` (transaction
    start). A renewal whose transaction began before the deadline but whose
    UPDATE only executes after the reaper already claimed the row must lose
    (issue #2053): with ``now()`` the guard would still pass on a session the
    kill was already dispatched for, and the row would show renewed while the
    session is dead.
    """
    cur.execute(
        "UPDATE agent_shell_ttls "
        "SET expires_at = clock_timestamp() + make_interval(secs => %s), "
        "renewals = renewals + 1, last_renewed_at = clock_timestamp() "
        "WHERE agent_id = %s AND session_id = %s AND expires_at > clock_timestamp() "
        "RETURNING expires_at",
        (ttl, agent_id, session_id),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def _apply_renewal(agent_id: int, session_id: int, ttl: float, prev_expires: datetime) -> datetime:
    """The guarded write: new deadline + audit row, one transaction.

    Returns the new deadline; raises ValueError when the row expired (or
    was reaped) between the read and this write — the owner renews before
    the deadline or not at all (user ruling 2026-09-08).
    """
    import psycopg

    from ava._settings import DB_URL
    from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

    new_expires: datetime | None = None
    try:
        with (
            psycopg.connect(DB_URL, prepare_threshold=None, **PG_STATEMENT_TIMEOUT_KWARGS) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SET TRANSACTION READ WRITE")
            updated = _renewal_update(cur, agent_id, session_id, ttl)
            if updated is not None:
                new_expires = updated
                cur.execute(
                    "INSERT INTO agent_shell_ttl_renewals "
                    "(agent_id, session_id, requested_ttl_seconds, prev_expires_at, "
                    "new_expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (agent_id, session_id, ttl, prev_expires, new_expires),
                )
                conn.commit()
    except psycopg.Error as exc:
        raise RuntimeError(f"failed to renew session {session_id}") from exc
    if new_expires is None:
        raise ValueError(
            f"session {session_id} is already past its TTL — expired sessions "
            "are reclaimed automatically; renew before the deadline"
        )
    return new_expires


def list() -> dict[int, str | None]:
    """Your sessions: id -> display name (None for unnamed).

    Entries named `page-<name>` are live page servers for pages this agent
    opened via `ava.ui.serve`, not leftovers; see `ava.ui` for their lifecycle.
    """
    prefix = _shell_prefix()
    out: dict[int, str | None] = {}
    for full in _own_sessions():
        rest = full[len(prefix) :]  # "<id>" or "<id>-<name>"
        sid, _, name = rest.partition("-")
        out[int(sid)] = name or None
    return out
