"""Cross-platform session backend — unifies the POSIX native process supervisor
and winproc (Windows) behind a common interface for long-running named process
sessions. Module-level ``get_backend()`` returns the platform-appropriate
singleton; callers use the same ``SessionBackend`` protocol regardless of
platform.

- ``get_backend()`` — the **service/daemon** backend: ``WinprocSessionBackend``
  on Windows; ``HelperProcSessionBackend`` on macOS when helper spawning is
  enabled; otherwise ``PosixProcSessionBackend`` (``shared.posixproc``) on
  POSIX. Every long-running service session lives here: ``ava start`` launches,
  healthcheck respawns, pause/unpause, and the orchestration sessions (updater /
  rollout / cluster-restart — S7 moved them onto this backend).
- ``get_shell_backend()`` — agent interactive shells / watchers:
  ``PtySessionBackend`` (one detached host per session) on POSIX, the native
  supervisor on Windows. Never addresses service or orchestration sessions.

**Every import of a platform supervisor in this module is deliberately
method-local** — `from shared import winproc` / `from shared import posixproc` /
`from shared.helperproc import HelperProcSessionBackend`
inside method bodies, never at module scope: the agent-runner self-update
(`cli/commands/_update_agent_runner.py`) calls `_do_stop` **in-process** after
`git checkout` + `uv sync`, so the session-kill code stop runs is whatever
`sys.modules` holds then. Deferring these imports — and this module staying
out of the updater's import closure — makes that stop load the just-pulled
killer off disk instead of the pre-pull one (PR #932's `winproc.kill_session`
fix reaches the rollout that ships it). Hoist any of them and a kill fix stops
reaching the rollout, with no symptom other than a Windows agent-runner that
stops and never comes back. Pinned by `tests/cli/test_update_import_timing.py`.
"""

from __future__ import annotations

import abc
import base64
import logging
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Protocol, cast

from shared.paths import logs_dir, run_dir

# ruff: noqa: S603 — subprocess calls use repo-internal literal args
from shared.platform import (
    IS_MACOS,
    IS_WINDOWS,
)
from shared.session_record import SessionRecord

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class SessionBackend(abc.ABC):
    """Abstract interface for long-running named process sessions."""

    @abc.abstractmethod
    def has_session(self, name: str) -> bool:
        """True if a session named ``name`` is currently alive (backend has-session)."""
        ...

    @abc.abstractmethod
    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        """Launch ``cmd`` as a detached, named background session.

        ``env`` is the **complete** child environment dict — the caller is
        responsible for building it (``shared.session_env.forward_env_dict`` for
        daemons, ``shared.session_env.agent_spawn_env_dict`` for agents).

        ``login_shell`` (POSIX only) wraps the command in ``bash -lc`` so
        user-local PATH additions are visible.  Windows ignores this flag —
        there is no login shell, and the native supervisor runs the command
        through cmd.exe only when its syntax requires one.

        ``exec_cmd`` (POSIX login shells only) makes that wrapper ``exec`` into
        the command, so the pid the supervisor records — and later SIGTERMs — is
        the command's own rather than a shell sitting in front of it. Daemons
        want this: a surviving wrapper swallows the graceful-stop signal and
        every stop runs to its full timeout. Pass False when the shell is itself
        part of the session's work — an orchestration session whose ``tee``
        pipeline and ``[session-exit] rc=`` verdict must outlive the command it
        runs (``ops.cluster_session``).

        Returns True on success. An existing live session of the same name is
        left untouched (idempotent), matching the existing guard at every call
        site.
        """
        ...

    @abc.abstractmethod
    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        """Stop a session by name.

        ``graceful=False`` is a force-kill; ``graceful=True`` sends an
        interrupt and waits up to ``timeout`` seconds for a clean exit before
        force-killing.

        ``expected`` marks an operator-initiated transition (rollout/update/
        stop): backends that escalate a kill (the native backend's SIGKILL chain) log the
        escalation at INFO instead of WARNING/ERROR there — the caller surfaces
        the outcome itself.

        Returns ``(ok, mode)`` where *mode* is one of ``{'graceful', 'forced',
        'noop'}``. Idempotent — killing an absent/dead session is a noop.

        ``ok`` means **the session is confirmed gone**, not "the kill command was
        accepted". A backend must re-ask its own existence check after killing and
        answer False when the session outlived it: the caller's next move is to
        launch that service again, and a kill that reports success it did not
        achieve turns a live-but-unbacked session into a service nothing starts
        (issue #1015).
        """
        ...

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool:
        """Send the backend's graceful-stop signal without waiting.

        Service backends override this for batch stop. Terminal-oriented
        backends intentionally have no such lifecycle contract.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support graceful_signal")

    @abc.abstractmethod
    def list_sessions(self, prefix: str = "") -> list[str]:
        """Names of all live sessions, optionally filtered by ``prefix`` (backend list)."""
        ...

    def session_started_at(self, name: str) -> float | None:  # noqa: ARG002 — base backend keeps no record
        """Epoch seconds when the session was launched, or None when it is not
        alive. Optional: a backend without a timestamp source answers None and
        consumers render no uptime for its sessions."""
        return None

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        """Launch epochs for MANY sessions in one call.

        Default: the per-session loop (`session_started_at` per name). A
        backend whose per-session read carries fixed overhead must
        override this with a batch call — a status snapshot fans out over
        every session serially, and 28 sessions blew past the roster's 3 s
        probe timeout, misreporting a healthy host offline."""
        return {name: self.session_started_at(name) for name in names}

    def session_generation(self, name: str) -> str | None:  # noqa: ARG002 — optional metadata
        """The live session's persisted generation, when this backend has one."""
        return None

    def session_log_path(self, name: str) -> Path | None:  # noqa: ARG002 — no log file for this backend
        """The file **this backend** redirects a session's output to, or None when it
        keeps no such file.

        Asked by anything that decides whether a long-running session is still
        working from the freshness of what it has written — currently
        ``ops.updater_reap._reap_stalled_updater``. A backend that keeps no file
        (the base default — a pane-shaped backend keeps no file) answers None; the
        native supervisors own their redirect and answer with it. A consumer that
        only knew about the tee'd file would have no liveness evidence at all on a
        backend that does not tee, which is how a stall timeout ends up
        structurally unable to fire on one platform.
        """
        return None

    # --- PTY-specific -------------------------------------------------------
    # Only backends that provide a terminal implement these.  Callers that need
    # PTY (ava.shell.sessions) currently use the PTY backend directly; these methods exist
    # so a future PTY-capable backend can plug in without changing callers.

    def send(self, name: str, text: str) -> None:
        """Type ``text`` WITHOUT Enter — the caller submits the line separately. PTY only."""
        raise NotImplementedError(f"{type(self).__name__} does not support send")

    def send_keys(self, name: str, *keys: str) -> None:
        """Send raw keys to a session's terminal. PTY only."""
        raise NotImplementedError(f"{type(self).__name__} does not support send_keys")

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        """Capture output from a session's terminal. PTY only."""
        raise NotImplementedError(f"{type(self).__name__} does not support capture_pane")

    def kill_session_with_verdict(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str, bool]:
        """Kill a session and report whether the kill interrupted running work.

        Returns ``(ok, mode, interrupted)`` — ``kill_session`` plus the TTL
        reaper's verdict: ``interrupted`` is True when the session carried
        live processes (a running foreground/background job) at kill time.
        The verdict is computed by the SAME operation that kills, so a job
        starting between a separate idle probe and the kill cannot be cut
        short without a notice. Backends that cannot produce a verdict raise
        NotImplementedError — the caller treats that as interrupted
        (fail-open: a session that cannot be proven idle may well be running
        something).
        """
        raise NotImplementedError(f"{type(self).__name__} does not support kill verdicts")


class NativeProcessSupervisor(Protocol):
    """Agent-process surface shared by the platform modules and helper backend."""

    __name__: str

    def has_session(self, name: str) -> bool: ...

    def new_session(
        self,
        name: str,
        cmd: str | list[str],
        cwd: Path,
        *,
        env: dict[str, str],
        stderr_append: Path | None = None,
    ) -> bool: ...

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0
    ) -> tuple[bool, str]: ...

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool: ...

    def list_sessions(self, prefix: str = "") -> list[str]: ...

    def session_log_path(self, name: str) -> Path | None: ...


# ── POSIX: native process supervisor ─────────────────────────────────────


class PosixProcSessionBackend(SessionBackend):
    """POSIX backend: long-running sessions managed by the native
    process supervisor (``shared.posixproc``) — the migration target for
    daemon/service sessions (gateway / frontend / health-checked daemons),
    which need no PTY. Agent *processes* already live on the same supervisor
    via ``native_proc()``.

    ``new_session`` mirrors the classic command shape so PATH/venv
    semantics match the legacy path: with ``login_shell=True`` (the default)
    the command runs as ``bash -lc 'cd <cwd> && <venv_activation_prefix>exec <cmd>'``
    — the login profile rebuilds PATH (macOS path_helper), dropping any venv
    prefix the forwarded env carried, so the venv is re-activated inside the
    command, exactly where the legacy backend did it. The ``exec``
    (``session_env.exec_into``) is what keeps the supervisor's recorded pid
    pointing at the daemon rather than at a surviving wrapper shell, so a
    graceful SIGTERM reaches the daemon. The caller-supplied
    ``env`` dict is handed straight to the supervisor as the child's real
    environment — no 0600 handoff file, because nothing is ever on an argv.

    PTY methods raise ``NotImplementedError`` — the native backend allocates no
    terminal; interactive shells (ava.shell.sessions, watchers) live in
    detached per-session hosts via ``get_shell_backend()``.

    Each method imports ``posixproc`` locally rather than at module scope, for
    the same self-update reason as ``WinprocSessionBackend`` — see the module
    docstring; ``tests/cli/test_update_import_timing.py`` fails if one is
    hoisted.
    """

    def has_session(self, name: str) -> bool:
        from shared import posixproc

        return posixproc.has_session(name)

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        from shared.session_env import exec_into, venv_activation_prefix

        if login_shell:
            body = exec_into(cmd) if exec_cmd else cmd
            inner = f"cd {cwd.as_posix()} && {venv_activation_prefix()}{body}"
            # The supervisor runs a string command as `/bin/sh -c <cmd>`, and
            # that `sh` is a SECOND shell in front of this one. bash-as-/bin/sh
            # (macOS) execs into a lone simple command on its own, but dash
            # (Linux) does not — so without this `exec` the recorded pid is that
            # `sh` on every Linux host and the graceful SIGTERM never gets past
            # it, exactly the way it did not get past the login shell. Always
            # safe: what follows is `bash -lc <one quoted arg>`, a simple
            # command by construction. Unconditional because the pid worth
            # recording is this login shell even when it must outlive its
            # command (exec_cmd=False).
            cmd = f"exec bash -lc {shlex.quote(inner)}"
        from shared import posixproc

        return posixproc.new_session(name, cmd, cwd, env=env)

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        # ``expected`` is accepted for interface parity; the native supervisor
        # has no force-kill escalation to quieten (its kill is the escalation).
        del expected
        from shared import posixproc

        return posixproc.kill_session(name, graceful=graceful, timeout=timeout)

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool:
        from shared import posixproc

        if expected is None:
            return posixproc.graceful_signal(name)
        return posixproc.graceful_signal(name, expected=expected)

    def list_sessions(self, prefix: str = "") -> list[str]:
        from shared import posixproc

        return posixproc.list_sessions(prefix)

    def session_started_at(self, name: str) -> float | None:
        from shared import posixproc

        return posixproc.session_started_at(name)

    def session_log_path(self, name: str) -> Path | None:
        from shared import posixproc

        return posixproc.session_log_path(name)


# ── POSIX: per-session PTY hosts ───────────────────────────────────────────
_PTY_CLI = "shared.pty_sessions.cli"  # python -m <this> <name> <op> [args]
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # env keys a POSIX shell can assign


def _write_session_env_file(env: dict[str, str]) -> Path:
    """Fresh 0600 env handoff file the per-session host sources."""

    directory = run_dir() / "session-env"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    body = ""
    for key, value in sorted(env.items()):
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        if "\0" in value:
            raise RuntimeError(f"env {key} value contains \\0 and cannot be forwarded")
        body += f"{key}={shlex.quote(value)}\n"
    path = directory / f"{uuid.uuid4().hex}.env.sh"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


class PtySessionBackend(SessionBackend):
    """POSIX backend for agent interactive shells / watchers — what
    ``get_shell_backend()`` returns on POSIX. Each session is an interactive
    login shell (``bash -l -i``, the classic pane shape) carried by its own
    detached host process (``shared.pty_sessions.host``), so no infra process
    holds every shell and sessions persist across agent exits, restarts, and
    cluster updates. ``cmd`` accepted but ignored; ``login_shell=False``
    raises ``NotImplementedError``. Env rides a 0600 file, never argv (#974).
    Liveness ops map the CLI exit status to the interface's bool/tuple/list
    shape; the enumeration ops read the session records in-process (no
    subprocess, no socket — task #1200's snapshot-cost fix, now structural).

    The mutating ops keep the CLI-subprocess shape rather than importing the
    pty package's internals: ``new`` must outlive nothing (the host detaches
    itself), and the subprocess boundary keeps this module import-light for
    the self-update window (``tests/cli/test_update_import_timing.py``).
    """

    def _cli(self, *tokens: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", _PTY_CLI, *tokens], capture_output=True, text=True, check=False
        )

    def has_session(self, name: str) -> bool:
        return self._cli(name, "has").returncode == 0

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,  # noqa: ARG002 — an interactive shell is never exec'd away
    ) -> bool:
        if not login_shell:
            raise NotImplementedError(f"{type(self).__name__} only creates login shells")
        envfile = _write_session_env_file(env)
        args = [name, "new", str(cwd), str(envfile)]
        if cmd:  # the per-session host submits the base64 command when ready
            args.append(base64.b64encode(cmd.encode()).decode("ascii"))
        result = self._cli(*args)
        if result.returncode != 0:
            envfile.unlink(missing_ok=True)
            detail = result.stderr.strip() or f"exit {result.returncode} without a diagnostic"
            _log.warning("pty session allocation failed for %s: %s", name, detail)
            return False
        return True

    def send(self, name: str, text: str) -> None:
        result = self._cli(name, "send", base64.b64encode(text.encode()).decode("ascii"))
        if result.returncode != 0:
            raise RuntimeError(f"pty CLI send {name!r} failed: {result.stderr.strip()}")

    def send_keys(self, name: str, *keys: str) -> None:
        result = self._cli(name, "send_keys", *keys)
        if result.returncode != 0:
            raise RuntimeError(f"pty CLI send_keys {name!r} failed: {result.stderr.strip()}")

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        args = (str(lines), "--scrollback") if scrollback else ("--no-scrollback",)
        result = self._cli(name, "capture", *args)
        if result.returncode != 0:
            raise RuntimeError(f"pty CLI capture {name!r} failed: {result.stderr.strip()}")
        return result.stdout

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        del timeout, expected  # the PTY CLI owns the graceful timeout
        result = self._cli(name, "kill", "--graceful") if graceful else self._cli(name, "kill")
        return result.returncode == 0, "graceful" if graceful else "forced"

    def kill_session_with_verdict(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str, bool]:
        del timeout, expected  # the PTY CLI owns the graceful timeout
        result = self._cli(name, "kill", "--graceful") if graceful else self._cli(name, "kill")
        ok = result.returncode == 0
        mode = "graceful" if graceful else "forced"
        # stdout carries the host's kill-time verdict ("interrupted"/"idle");
        # anything else (record-based fallback kill, wedged host) is not
        # proven idle — report interrupted (fail-open).
        interrupted = result.stdout.strip() != "idle"
        return ok, mode, interrupted

    def list_sessions(self, prefix: str = "") -> list[str]:
        """Live session names from the record scan — no subprocess, no socket.

        The records under ``$AVA_HOME/run/pty/`` ARE the session listing (one
        per live host); a record whose shell is gone is swept as it is
        discovered. There is no daemon whose downtime could blank this view.
        """
        from shared.pty_sessions.cli import live_sessions

        return sorted(live_sessions(prefix))

    def session_started_at(self, name: str) -> float | None:
        """Epoch seconds the named pty session was launched, or None when it
        is not alive — the CLI's record + shell-pid liveness rule, read
        in-process (no subprocess, task #1200)."""
        from shared.pty_sessions.cli import session_started_at

        return session_started_at(name)

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        """Launch epochs for MANY sessions in one record scan — no subprocess
        (the per-session CLI path costs one python startup each, and a status
        snapshot queries every session serially: ~0.58s per CLI invocation on
        a WSL runner, measured 2026-08-12, task #1200).

        Falls back to individual record reads when the directory scan fails
        with an I/O error, preserving the pre-batch behavior."""
        if not names:
            return {}
        from shared.pty_sessions.cli import live_sessions

        try:
            live = live_sessions()
        except OSError:
            return super().session_started_ats(names)
        return {name: (live[name].started_at if name in live else None) for name in names}

    def session_generation(self, name: str) -> str | None:
        """The live PTY record's flip generation, or None for legacy records."""
        from shared.pty_sessions.cli import session_generation

        return session_generation(name)

    def session_log_path(self, name: str) -> Path | None:
        return logs_dir() / f"{name}.out.log"


# ---------------------------------------------------------------------------
# Windows: native process supervisor
# ---------------------------------------------------------------------------


class WinprocSessionBackend(SessionBackend):
    """Windows backend: long-running sessions managed by the native process
    supervisor (``shared.winproc``).

    ``new_session`` hands the command to the supervisor with the caller-supplied
    ``env`` dict; the supervisor decides whether cmd.exe is needed to run it (see
    ``shared.winproc._plan_launch`` — the choice governs whether the command's
    output survives).  PTY methods raise ``NotImplementedError``.

    Each method imports ``winproc`` locally rather than at module scope, and that is
    load-bearing, not style: it is what lets the self-update's post-checkout stop
    kill with the freshly pulled ``kill_session``. See the module docstring;
    ``tests/cli/test_update_import_timing.py`` fails if one of these is hoisted.
    """

    def has_session(self, name: str) -> bool:
        from shared import winproc

        return winproc.has_session(name)

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,  # noqa: ARG002 — no login shell exists on Windows
        exec_cmd: bool = True,  # noqa: ARG002 — cmd.exe has no exec
    ) -> bool:
        from shared import winproc

        # `.venv/bin/python` -> the checkout's Windows interpreter is the
        # supervisor's job: it rewrites the token *after* splitting the command,
        # so a checkout path containing a space survives. Rewriting the raw
        # string here would splice an unquoted path back into it.
        return winproc.new_session(name, cmd, cwd, env=env)

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        # ``expected`` is accepted for interface parity; the native supervisor
        # has no force-kill escalation to quieten (its kill is the escalation).
        del expected
        from shared import winproc

        return winproc.kill_session(name, graceful=graceful, timeout=timeout)

    def graceful_signal(
        self, name: str, *, expected: SessionRecord | None = None, timeout: float = 5.0
    ) -> bool:
        from shared import winproc

        if expected is None and timeout == 5.0:
            return winproc.graceful_signal(name)
        return winproc.graceful_signal(name, expected=expected, timeout=timeout)

    def list_sessions(self, prefix: str = "") -> list[str]:
        from shared import winproc

        return winproc.list_sessions(prefix)

    def session_started_at(self, name: str) -> float | None:
        from shared import winproc

        return winproc.session_started_at(name)

    def session_log_path(self, name: str) -> Path | None:
        from shared import winproc

        return winproc.session_log_path(name)

    def kill_session_with_verdict(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str, bool]:
        del expected
        from shared import winproc

        return winproc.kill_session_with_verdict(name, graceful=graceful, timeout=timeout)


_backend: SessionBackend | None = None


def helper_spawn_enabled() -> bool:
    """Whether macOS process creation must route through the permissions helper.

    Configuration failure keeps the legacy POSIX route. Choosing the helper is
    a spawn-identity commitment, so actual helper-call failures remain loud and
    never fall back after this decision.
    """
    if not IS_MACOS:
        return False
    try:
        from shared.config import settings

        return bool(
            settings.services.permissions_helper_enabled
            and settings.services.permissions_helper_spawn
        )
    except Exception:
        return False


def get_backend() -> SessionBackend:
    """Return the platform-appropriate ``SessionBackend`` singleton.

    This is the **service/daemon** backend — every long-running service session
    (`ava start` launches, healthcheck respawns, pause/unpause) and every
    orchestration session (updater / rollout / cluster-restart) lives here:
    the helper-backed supervisor on opted-in macOS hosts, the native supervisor
    on other POSIX hosts, and the native supervisor on Windows. Agent
    *processes* do NOT call this function directly: `native_proc()` routes them
    to the same selected process supervisor. Agent shells / watchers use the
    PTY backend (`get_shell_backend()`).
    """
    global _backend  # noqa: PLW0603
    if _backend is None:
        if IS_WINDOWS:
            _backend = WinprocSessionBackend()
        elif helper_spawn_enabled():
            from shared.helperproc import HelperProcSessionBackend

            _backend = HelperProcSessionBackend()
        else:
            _backend = PosixProcSessionBackend()
    return _backend


_shell_backend: SessionBackend | None = None


def get_shell_backend() -> SessionBackend:
    """Return the backend for AGENT interactive shells and watchers —
    ``PtySessionBackend`` on POSIX (one detached host per session), the
    native supervisor on Windows; distinct from ``get_backend()``
    (service/daemon + orchestration sessions). ``ava.shell.sessions`` and
    watcher sessions use this PTY backend — never the service backend.
    """
    global _shell_backend  # noqa: PLW0603
    if _shell_backend is None:
        _shell_backend = WinprocSessionBackend() if IS_WINDOWS else PtySessionBackend()
    return _shell_backend


def native_proc() -> NativeProcessSupervisor:
    """The platform's native process supervisor for AGENT processes —
    `shared.winproc` (Windows), the helper-backed service backend on opted-in
    macOS hosts, or `shared.posixproc` (other POSIX routes).

    Both modules expose the same surface (`has_session` / `new_session` /
    `kill_session` / `list_sessions` / `session_log_path`), so `ops.agent_launch`
    (attempt launch) and the reap / force-terminate / status consumers
    dispatch to one of the two by platform. Agent processes always run here (a
    non-interactive agent needs no PTY, and the per-box PTY ceiling then stops
    bounding agent count); daemons use `get_backend()`, while agents'
    persistent shells use `get_shell_backend()`.

    Supervisor imports are method-local for the same reason as
    `WinprocSessionBackend`'s:
    `ava stop`'s agent reap (`cli.commands.stop._reap_agent_sessions`) runs through
    here, and on the self-update path that reap must be the post-checkout code.
    """
    if IS_WINDOWS:
        from shared import winproc

        return cast("NativeProcessSupervisor", winproc)
    if helper_spawn_enabled():
        return cast("NativeProcessSupervisor", get_backend())
    from shared import posixproc

    return cast("NativeProcessSupervisor", posixproc)
