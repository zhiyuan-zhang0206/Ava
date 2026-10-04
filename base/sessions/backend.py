"""Session backends for long-running named processes and agent shells.

- ``get_backend()`` — the **service/daemon** backend: ``HelperProcSessionBackend``
  on macOS when helper spawning is enabled; otherwise ``PosixProcSessionBackend``.
- ``get_shell_backend()`` — agent interactive shells / watchers:
  ``PtySessionBackend`` (the pty-sessions service). Never addresses service sessions.

Platform supervisor imports are method-local so selecting one backend does not
import every implementation.
"""

from __future__ import annotations

import abc
import logging
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from base.native_process.os_platform import IS_MACOS
from base.paths import logs_dir
from base.sessions.pty import client
from base.sessions.pty.keys import keys_to_bytes
from base.sessions.record import SessionRecord

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
        responsible for building it (``base.sessions.env_forwarding.forward_env_dict`` is the
        shared builder every current caller uses).

        ``login_shell`` wraps the command in ``bash -lc`` so user-local PATH
        additions are visible.

        ``exec_cmd`` makes that wrapper ``exec`` into
        the command, so the pid the supervisor records — and later SIGTERMs — is
        the command's own rather than a shell sitting in front of it. Daemons
        want this: a surviving wrapper swallows the graceful-stop signal and
        every stop runs to its full timeout. Pass False when the shell is itself
        part of the session's work — a session whose ``tee`` pipeline and
        ``[session-exit] rc=`` verdict must outlive the command it runs.

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

        Asked by anything that reads a long-running session's output from where
        the backend wrote it — currently the schedule log reader
        (``gateway.schedules.router``). A backend that keeps no file
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
    process supervisor (``base.sessions.posixproc``) — the migration target for
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
    the pty-sessions service via ``get_shell_backend()``.

    Each method imports ``posixproc`` locally rather than at module scope — see
    the module docstring.
    """

    def has_session(self, name: str) -> bool:
        from base.sessions import posixproc

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
        from base.sessions.env_forwarding import exec_into, venv_activation_prefix

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
        from base.sessions import posixproc

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
        from base.sessions import posixproc

        return posixproc.kill_session(name, graceful=graceful, timeout=timeout)

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool:
        from base.sessions import posixproc

        if expected is None:
            return posixproc.graceful_signal(name)
        return posixproc.graceful_signal(name, expected=expected)

    def list_sessions(self, prefix: str = "") -> list[str]:
        from base.sessions import posixproc

        return posixproc.list_sessions(prefix)

    def session_started_at(self, name: str) -> float | None:
        from base.sessions import posixproc

        return posixproc.session_started_at(name)

    def session_log_path(self, name: str) -> Path | None:
        from base.sessions import posixproc

        return posixproc.session_log_path(name)


# ── POSIX: the pty-sessions service ────────────────────────────────────────


class PtySessionBackend(SessionBackend):
    """POSIX backend for agent interactive shells / watchers — what
    ``get_shell_backend()`` returns on POSIX. Each session is an interactive
    login shell (``bash -l -i``, the classic pane shape) held by the machine's
    pty-sessions service (``services.agent_runner.pty_sessions``), a roster service, so a
    session persists across agent exits and agent-host restarts, and a service
    stop closes it. ``cmd`` is submitted after the shell is ready;
    ``login_shell=False`` raises ``NotImplementedError``. The shell's environment
    is the service's own overlaid with ``env`` (omitted: the standard projection,
    with venv activation only for cwd inside this checkout), sent in the request
    body of a 0600 socket, never on an argv (#974).

    Every call is one short connection to the service (``base.sessions.pty.client``),
    so a restarted agent simply dials again. A service that is not running holds no
    sessions: the queries answer none and ``kill`` is a noop; ``new``, ``send`` and
    ``capture`` raise ``RuntimeError``. The interface's bool/tuple/list shapes are
    mapped from the service's answers.
    """

    def has_session(self, name: str) -> bool:
        return client.has_session(name)

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        login_shell: bool = True,
        exec_cmd: bool = True,  # noqa: ARG002 — an interactive shell is never exec'd away
    ) -> bool:
        if not login_shell:
            raise NotImplementedError(f"{type(self).__name__} only creates login shells")
        if env is None:
            from base.paths import repo_root
            from base.sessions.env_forwarding import cwd_is_inside_checkout, forward_env_dict

            env = forward_env_dict(activate_venv=cwd_is_inside_checkout(cwd, repo_root()))
        try:
            client.create_session(name, str(cwd), env, cmd or None)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            _log.warning("pty session allocation failed for %s: %s", name, exc)
            return False
        return True

    def send(self, name: str, text: str) -> None:
        self._call("send", name, client.send, name, text.encode())

    def send_keys(self, name: str, *keys: str) -> None:
        self._call("send_keys", name, client.send, name, keys_to_bytes(keys))

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        return self._call("capture", name, client.capture, name, lines, scrollback=scrollback)

    @staticmethod
    def _call[T](op: str, name: str, call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run one service call; any failure is a RuntimeError naming the op and session."""
        try:
            return call(*args, **kwargs)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            raise RuntimeError(f"pty {op} {name!r} failed: {exc}") from exc

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        ok, mode, _interrupted = self.kill_session_with_verdict(
            name, graceful=graceful, timeout=timeout, expected=expected
        )
        return ok, mode

    def kill_session_with_verdict(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str, bool]:
        del timeout, expected  # the service owns the graceful timeout
        try:
            verdict = client.kill(name, graceful=graceful)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            _log.warning("pty kill of %s failed: %s", name, exc)
            # A session that cannot be proven idle may well be running something (fail-open).
            return False, "graceful" if graceful else "forced", True
        return True, verdict.mode, verdict.interrupted

    def list_sessions(self, prefix: str = "") -> list[str]:
        """Live session names, as the service lists them; none when it is not running."""
        return [info.name for info in client.list_sessions(prefix)]

    def session_started_at(self, name: str) -> float | None:
        """Epoch seconds the named pty session was launched, or None when it is not alive."""
        return self.session_started_ats([name])[name]

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        """Launch epochs for MANY sessions in one call to the service: a status snapshot
        asks about every session, and one round trip each would be the cost the
        snapshot's probe timeout cannot afford (task #1200)."""
        if not names:
            return {}
        started = {info.name: info.started_at for info in client.list_sessions()}
        return {name: started.get(name) for name in names}

    def session_generation(self, name: str) -> str | None:
        """The live PTY session's allocation generation, or None for none."""
        for info in client.list_sessions(name):
            if info.name == name:
                return info.generation
        return None

    def session_log_path(self, name: str) -> Path | None:
        return logs_dir() / f"{name}.out.log"


def helper_spawn_enabled() -> bool:
    """Whether macOS process creation must route through the permissions helper.

    Configuration failure keeps the legacy POSIX route. Choosing the helper is
    a spawn-identity commitment, so actual helper-call failures remain loud and
    never fall back after this decision.
    """
    if not IS_MACOS:
        return False
    try:
        from base.config import settings

        return bool(
            settings.services.permissions_helper_enabled
            and settings.services.permissions_helper_spawn
        )
    except Exception:
        _log.warning(
            "reading the permissions-helper settings failed; routing process creation "
            "through the legacy POSIX supervisor",
            exc_info=True,
        )
        return False


def get_backend() -> SessionBackend:
    """Return the platform-appropriate ``SessionBackend`` for services/daemons.

    This is the **service/daemon** backend — every long-running service session
    (`ava start` launches, pause/unpause) lives here: the helper-backed
    supervisor on opted-in macOS hosts, the native supervisor on other POSIX
    hosts. Both are stateless handles over the supervisor's on-disk records, so a
    fresh one per call is the same backend. Agent
    *processes* do NOT call this function directly: `native_proc()` routes them
    to the same selected process supervisor. Agent shells / watchers use the
    PTY backend (`get_shell_backend()`).
    """
    if helper_spawn_enabled():
        from base.sessions.helperproc import HelperProcSessionBackend

        return HelperProcSessionBackend()
    return PosixProcSessionBackend()


def get_shell_backend() -> SessionBackend:
    """Return the backend for AGENT interactive shells and watchers —
    ``PtySessionBackend`` (the pty-sessions service, stateless: every call dials
    it); distinct from ``get_backend()`` (service/daemon sessions).
    ``ava.shell.sessions`` and watcher sessions use this PTY backend — never the
    service backend.
    """
    return PtySessionBackend()


def native_proc() -> NativeProcessSupervisor:
    """The platform's native process supervisor for AGENT processes —
    The helper-backed service backend on opted-in macOS hosts, or
    `base.sessions.posixproc` on other hosts.

    Both modules expose the same surface (`has_session` / `new_session` /
    `kill_session` / `list_sessions` / `session_log_path`), so the agent launch
    and the reap / force-terminate / status consumers
    dispatch to one of the two by platform. Agent processes always run here (a
    non-interactive agent needs no PTY, and the per-box PTY ceiling then stops
    bounding agent count); daemons use `get_backend()`, while agents'
    persistent shells use `get_shell_backend()`.

    Supervisor imports are local for the same reason as the module's backends:
    selecting one platform does not import every platform implementation.
    """
    if helper_spawn_enabled():
        return cast("NativeProcessSupervisor", get_backend())
    from base.sessions import posixproc

    return cast("NativeProcessSupervisor", posixproc)
