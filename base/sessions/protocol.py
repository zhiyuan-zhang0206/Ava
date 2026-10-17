"""The abstract session-backend interface every process and shell backend implements."""

from __future__ import annotations

import abc
from pathlib import Path

from base.sessions.record import SessionRecord


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

        For a PTY, success confirms shell/session closure, not the absence of all
        background or detached work. ``ok`` means **the session is confirmed gone**,
        not "the kill command was
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

    # meiva:link base/sessions/docs/host-generation.meiva.okf.md
    def host_generation(self) -> str | None:
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
    # PTY (meiva.shell.sessions) currently use the PTY backend directly; these methods exist
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
