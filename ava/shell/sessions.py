"""Persistent shell sessions that preserve cwd, env, and background processes across calls."""

__all_for_ava__ = ["capture", "kill", "list", "new", "send", "send_keys"]

import builtins
import contextlib
import re
from pathlib import Path

import ava
import ava._boot
from ava.security import scan_content
from shared.cluster import session_name
from shared.paths import workspace_dir
from shared.session_backend import get_shell_backend
from shared.session_env import forward_env_dict


def _agent_prefix() -> str:
    # Generic prefix for all shell sessions of the current agent:
    # `ava-agent-<agent_id>-`. The agent's process, its shells, and
    # its watchers all share this prefix; kill_all filters on it.
    return f"{session_name(f'agent-{ava._boot.agent_id()}')}-"


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

    agent_id = ava._boot.agent_id()
    if agent_id is None:  # type: ignore[unnecessary-isinstance]  # _boot.agent_id() returns None pre-bootstrap (type annotation is int for call-site simplicity)
        raise RuntimeError(
            "Cannot allocate a session index: this process has no agent identity. "
            "ava.shell.sessions.new() requires an agent process or a background "
            "script launched by one (which receives the identity via "
            "ava._boot.establish). Running a standalone script that imports ava "
            "does not set an agent identity."
        )
    # PG_KEEPALIVE_KWARGS: this runs inside the agent's exec sandbox, so a
    # black-holing database would otherwise hang `ava.shell.new()` on the OS
    # TCP-retransmit timeout instead of raising. Same constant as shared.db.
    with psycopg.connect(DB_URL, **PG_STATEMENT_TIMEOUT_KWARGS) as conn, conn.cursor() as cur:
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


def _create_session(name: str | None = None, *, cwd: str | None = None) -> tuple[int, str]:
    # Allocate the next session id and create the shell session. `name` becomes
    # a `-<name>` suffix on the session identifier (None = unnamed). `cwd` sets
    # the session's starting directory (None = the agent's workspace, the same
    # base ava.shell.run uses). Returns (id, full_name). Internal — `new()`
    # (shell), `run_background`, and `ava.watcher._spawn` (named "watcher")
    # all use it.
    if name is not None and not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"session name {name!r} invalid — use a lowercase slug like 'dev-server' "
            "([a-z][a-z0-9-]*)"
        )
    session_id = _next_session_index_from_db()
    full = f"{_shell_prefix()}{session_id}" + (f"-{name}" if name is not None else "")
    # Forward this agent process's AVA_* env onto the session. The PTY
    # supervisor daemon starts the login shell with its OWN env (frozen at
    # daemon start — a host-wide process), so without this a shell or watcher
    # would inherit whatever cluster the daemon was started under — binding to
    # the wrong cluster on a host running more than one. The agent's own env is
    # authoritative (the gateway forwarded the correct cluster into it at
    # spawn), so re-forwarding it keeps every child on the same cluster.
    #
    # It rides a 0600 envfile the backend writes, NOT argv: the env carries the
    # agent's provider keys and data-plane URLs, and argv is world-readable
    # through `ps` (issue #974).
    backend = get_shell_backend()
    if cwd is None:
        agent_id = ava._boot.agent_id()
        # agent id is typed int but is None until a bootstrap establishes it
        # (same fallback as ava.shell.run — the DB call above already raised
        # pre-bootstrap, so this is only about resolving the base).
        cwd = str(workspace_dir(agent_id)) if agent_id is not None else str(Path.home())  # pyright: ignore[reportUnnecessaryComparison]
    ok = backend.new_session(full, "", Path(cwd), env=forward_env_dict())
    if not ok:
        raise RuntimeError(f"failed to create session {full!r}")
    return session_id, full


def new(name: str) -> int:
    """Create a session and return its id. `name` is only a display label —
    a lowercase slug like `"dev-server"`; every operation takes the id."""
    session_id, _ = _create_session(name)
    return session_id


# `id` / `list` shadow builtins intentionally: these are the agent-facing names.
# (flake8-builtins `A` is not in this repo's ruff select, so no noqa is needed.)
def send(id: int, cmd: str, *, enter: bool = True) -> None:
    """Type `cmd` into the session and submit it. Asynchronous — returns
    immediately without waiting for the command.

    Set `enter=False` to type the string without pressing Enter."""
    # Text and Enter go in separate writes: a combined write races TUI
    # programs (Claude Code, Codex) that are still processing the typed text
    # when Enter arrives.
    backend = get_shell_backend()
    target = _resolve(id)
    backend.send(target, cmd)
    if enter:
        backend.send_keys(target, "Enter")


def send_keys(id: int, *keys: str) -> None:
    """Send raw keys to a session without submitting a line — for driving
    interactive programs. Each argument is one key: a single character, or a
    name like `C-c`, `Escape`, `Up`, `Enter`, `Space`, `PageUp`."""
    get_shell_backend().send_keys(_resolve(id), *keys)


def capture(id: int, lines: int = 200, *, scrollback: bool = True) -> str:
    """Return the session's most recent `lines` of output, including history
    that has scrolled past. Pass `scrollback=False` to get only the current
    visible screen instead — needed for full-screen programs that redraw in
    place (`lines` is ignored then)."""
    # A session holds whatever ran in it — an interactive fetch, a coding agent
    # rendering a web page — so reading one ingests exactly as `shell.run` does.
    # Scanned for the same reason; the text comes back byte-for-byte.
    name = _resolve(id)
    pane = get_shell_backend().capture_pane(name, lines, scrollback=scrollback)
    return scan_content(pane, source="shell.sessions.capture")


def kill(id: int) -> None:
    backend = get_shell_backend()
    ok, _mode = backend.kill_session(_resolve(id), graceful=False)
    if not ok:
        raise RuntimeError(f"failed to kill session {id}")
    # R1 (Task #1021): a deliberately killed watcher must not be rebuilt by the
    # next boot reconcile — drop its registry row. A non-watcher session has no
    # row, so this is a no-op there. Fail-soft: a registry blip must not make
    # the kill itself fail.
    with contextlib.suppress(Exception):
        from ava import _boot
        from shared.watcher_registry import delete_watcher

        delete_watcher(int(_boot.agent_id()), id)


# Not in __all_for_ava__, so never rendered into the SDK docs: a prefix-scoped
# cleanup helper used by test fixtures. The agent exit path deliberately does
# NOT reap sessions — they outlive the process (see agent/loop.py); the agent
# itself kills sessions one by one via kill(id).
def kill_all() -> int:
    sessions = _own_sessions()
    for name in sessions:
        with contextlib.suppress(RuntimeError):
            get_shell_backend().kill_session(name, graceful=False)
    return len(sessions)


def list() -> dict[int, str | None]:
    """Your sessions: id -> display name (None for unnamed)."""
    prefix = _shell_prefix()
    out: dict[int, str | None] = {}
    for full in _own_sessions():
        rest = full[len(prefix) :]  # "<id>" or "<id>-<name>"
        sid, _, name = rest.partition("-")
        out[int(sid)] = name or None
    return out
