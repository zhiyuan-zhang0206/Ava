"""Cross-platform OS abstraction — unifies macOS, Windows, and Linux platform
differences behind a common interface, following the ``shared/session_backend.py``
provider pattern.

Module-level ``get_backend()`` returns the platform-appropriate singleton.
Callers use the same ``PlatformBackend`` protocol regardless of platform;
the backend is selected by the canonical flags in ``shared.platform``
(``IS_MACOS`` / ``IS_LINUX`` / ``IS_WINDOWS``).

Design:
  - Abstract methods are the operations that differ by platform.
  - Concrete capability-query methods (``supports_*``) have sensible defaults
    that each subclass can override.
  - The Windows backend returns a no-op for the one feature not yet wired
    (data-plane), so callers need no ``if IS_WINDOWS`` guards.
"""

from __future__ import annotations

import abc
import sys
from pathlib import Path

from shared.platform import IS_MACOS, IS_WINDOWS

# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class PlatformBackend(abc.ABC):
    """Abstract interface for OS-platform operations.

    Each method that differs by platform is abstract; a backend for a new
    platform is one new class implementing every abstract method.
    """

    # -- venv ---------------------------------------------------------------

    @abc.abstractmethod
    def venv_bin_dir_name(self) -> str:
        """The name of the virtualenv binary directory — ``"bin"`` (POSIX) or
        ``"Scripts"`` (Windows)."""
        ...

    def venv_python(self) -> str:
        """Absolute path to the Python interpreter inside the repo's virtualenv."""
        from shared.runtime_interpreter import runtime_venv

        return str(runtime_venv() / self.venv_bin_dir_name() / "python3")

    def venv_launcher(self, name: str, *, root: Path | None = None) -> Path:
        """Absolute path to a console script the virtualenv installs (``"ava"``,
        ``"uv"``, …) — ``.venv/bin/<name>`` on POSIX, ``.venv\\Scripts\\<name>.exe``
        on Windows.

        `root` is the checkout holding the ``.venv`` (defaults to this one's repo
        root); the agent-runner self-update passes the repo it is upgrading. The
        Windows suffix is not cosmetic: a caller that hands the extension-less
        path to ``subprocess`` is relying on CreateProcess's PATHEXT search, which
        does not apply to an absolute path with a directory component.
        """
        from shared.runtime_interpreter import runtime_venv

        return runtime_venv(checkout=root) / self.venv_bin_dir_name() / name

    # -- autostart ----------------------------------------------------------

    @abc.abstractmethod
    def register_autostart(self) -> None:
        """Register a boot-time job that runs ``ava start`` on reboot.

        macOS: launchd RunAtLoad LaunchAgent plist.
        Linux: a distro-level systemd boot unit when installed and enabled
        (``shared.os_boot_unit``), else an ``@reboot`` crontab entry.
        Windows: Task Scheduler ``/SC ONLOGON`` job.

        Reached only through ``shared.os_autostart.register_autostart``, which
        applies the ``os_jobs_enabled()`` gate — call that, not this.

        Idempotent. Raises ``RuntimeError`` on registration failure.
        """
        ...

    @abc.abstractmethod
    def unregister_autostart(self, slug: str) -> None:
        """Remove the boot-time autostart job of the cluster whose home slug is
        ``slug``. Safe when none is registered.

        The slug is passed in rather than re-derived from ``$AVA_HOME``: the only
        caller that removes *another* cluster's jobs (``ava cluster destroy``)
        runs inside a process whose own settings were frozen at import.
        """
        ...

    # -- cron ---------------------------------------------------------------

    @abc.abstractmethod
    def register_cron(self, interval_s: int = 300, threshold: int = 3) -> None:
        """Register the periodic OS cron job for the cluster health probe.

        macOS: launchd StartInterval LaunchAgent plist.
        Linux: user crontab entry.
        Windows: Task Scheduler ``/SC MINUTE`` job.

        Reached only through ``shared.os_cron.register_os_cron``, which applies
        the ``os_jobs_enabled()`` gate — call that, not this.

        Idempotent — re-running updates the interval/threshold. Raises
        ``RuntimeError`` on registration failure.
        """
        ...

    @abc.abstractmethod
    def unregister_cron(self, slug: str) -> None:
        """Remove the health-probe cron job of the cluster whose home slug is
        ``slug``. Safe when none is registered."""
        ...

    # -- logs maintenance ----------------------------------------------------

    @abc.abstractmethod
    def register_logs_job(self) -> None:
        """Register daily copytruncate rotation followed by tiered retention."""
        ...

    @abc.abstractmethod
    def unregister_logs_job(self, slug: str) -> None:
        """Remove the daily logs-maintenance job for ``slug``."""
        ...

    # -- packages refresh ----------------------------------------------------

    @abc.abstractmethod
    def register_packages_job(self) -> None:
        """Register the recurring content-refresh pass (skills fast lane)."""
        ...

    @abc.abstractmethod
    def unregister_packages_job(self, slug: str) -> None:
        """Remove the recurring content-refresh pass for ``slug``."""
        ...

    # -- pr flow -------------------------------------------------------------

    @abc.abstractmethod
    def register_pr_flow_job(self) -> None:
        """Register the daily PR-flow sampler job (task #2139).

        Reached only through ``shared.os_pr_flow.register_pr_flow_job``, which
        applies the ``os_jobs_enabled()`` gate plus the credential gate (gh +
        Trunk token + production home) — call that, not this.

        Idempotent — re-running replaces the definition. Raises ``RuntimeError``
        on registration failure (POSIX). Windows carries no registration path
        for a script-mode command and stays a no-op.
        """
        ...

    @abc.abstractmethod
    def unregister_pr_flow_job(self, slug: str) -> None:
        """Remove the daily PR-flow sampler job for ``slug``. Safe when none is
        registered."""
        ...

    # -- process ------------------------------------------------------------

    @abc.abstractmethod
    def process_alive(self, pid: int) -> bool:
        """True if ``pid`` names a live process on this host.

        POSIX: ``os.kill(pid, 0)`` (signal 0 — existence probe).
        Windows: ``psutil.pid_exists(pid)`` (TerminateProcess hazard avoided).
        """
        ...

    @abc.abstractmethod
    def force_kill(self, pid: int) -> None:
        """Force-terminate ``pid``. A dead/absent pid is a silent no-op.

        POSIX: ``os.kill(pid, SIGKILL)``.
        Windows: ``psutil.Process(pid).kill()`` (TerminateProcess).
        """
        ...

    # -- PostgreSQL ---------------------------------------------------------

    @abc.abstractmethod
    def pg_binary_path(self, name: str) -> Path | None:
        """Resolve a PostgreSQL binary name to a full path, or ``None`` when
        the host has no known installation.

        The vendored relocatable Postgres (``shared.runtime_binaries``) is
        checked first; only when absent does the platform default apply.
        """
        ...

    # -- capability queries -------------------------------------------------

    def supports_ava_symlink(self) -> bool:
        """``True`` when the ``~/.local/bin/ava`` symlink model works."""
        return True

    def supports_shell_rc(self) -> bool:
        """``True`` when ``~/.zshrc`` / ``~/.bashrc`` PATH editing works."""
        return True

    def is_posix(self) -> bool:
        """``True`` on POSIX platforms — the shell-flavored session model
        (login-shell wrapping, POSIX signals) is available."""
        return True

    def supports_data_plane(self) -> bool:
        """``True`` when this host can run a native per-cluster Postgres+Redis
        data plane (``pg_ctl`` + ``redis-server`` on PATH)."""
        return True

    def npm_shell_flag(self) -> bool:
        """``True`` when ``npm`` commands need ``shell=True`` so the platform
        resolves ``npm.cmd`` (Windows)."""
        return False


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


class MacPlatformBackend(PlatformBackend):
    """macOS backend (darwin)."""

    # -- venv --

    def venv_bin_dir_name(self) -> str:
        return "bin"

    # -- autostart --

    def register_autostart(self) -> None:
        from shared.os_autostart import _register_macos

        rc = _register_macos()
        if rc != 0:
            raise RuntimeError("autostart registration failed on macOS")

    def unregister_autostart(self, slug: str) -> None:
        from shared.os_autostart import _unregister_macos

        _unregister_macos(slug)

    # -- cron --

    def register_cron(self, interval_s: int = 300, threshold: int = 3) -> None:
        from shared.os_cron import _register_macos

        rc = _register_macos(interval_s, threshold)
        if rc != 0:
            raise RuntimeError("cron registration failed on macOS")

    def unregister_cron(self, slug: str) -> None:
        from shared.os_cron import _unregister_macos

        _unregister_macos(slug)

    # -- logs maintenance --

    def register_logs_job(self) -> None:
        from shared.os_logs_job import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("logs-maintenance registration failed on macOS")

    def unregister_logs_job(self, slug: str) -> None:
        from shared.os_logs_job import _unregister_macos

        _unregister_macos(slug)

    # -- packages refresh --

    def register_packages_job(self) -> None:
        from shared.os_packages import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("packages-refresh registration failed on macOS")

    def unregister_packages_job(self, slug: str) -> None:
        from shared.os_packages import _unregister_macos

        _unregister_macos(slug)

    # -- pr flow --

    def register_pr_flow_job(self) -> None:
        from shared.os_pr_flow import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("PR-flow registration failed on macOS")

    def unregister_pr_flow_job(self, slug: str) -> None:
        from shared.os_pr_flow import _unregister_macos

        _unregister_macos(slug)

    # -- process --

    def process_alive(self, pid: int) -> bool:
        import os

        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def force_kill(self, pid: int) -> None:
        import os

        from shared.platform import SIGKILL

        try:
            os.kill(pid, SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        from shared.pg_tools import brew_prefix

        return brew_prefix("postgresql@17") / "bin" / name


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


class LinuxPlatformBackend(PlatformBackend):
    """Linux backend."""

    # -- venv --

    def venv_bin_dir_name(self) -> str:
        return "bin"

    # -- autostart --

    def register_autostart(self) -> None:
        """Systemd boot unit when installed+enabled, else the cron entry --
        the branch lives in `shared.os_autostart._register_linux`."""
        from shared.os_autostart import _register_linux

        rc = _register_linux()
        if rc != 0:
            raise RuntimeError("autostart registration failed on Linux")

    def unregister_autostart(self, slug: str) -> None:
        from shared.os_autostart import _unregister_linux

        _unregister_linux(slug)

    # -- cron --

    def register_cron(self, interval_s: int = 300, threshold: int = 3) -> None:
        from shared.os_cron import _register_linux

        rc = _register_linux(interval_s, threshold)
        if rc != 0:
            raise RuntimeError("cron registration failed on Linux")

    def unregister_cron(self, slug: str) -> None:
        from shared.os_cron import _unregister_linux

        _unregister_linux(slug)

    # -- logs maintenance --

    def register_logs_job(self) -> None:
        from shared.os_logs_job import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("logs-maintenance registration failed on Linux")

    def unregister_logs_job(self, slug: str) -> None:
        from shared.os_logs_job import _unregister_linux

        _unregister_linux(slug)

    # -- packages refresh --

    def register_packages_job(self) -> None:
        from shared.os_packages import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("packages-refresh registration failed on Linux")

    def unregister_packages_job(self, slug: str) -> None:
        from shared.os_packages import _unregister_linux

        _unregister_linux(slug)

    # -- pr flow --

    def register_pr_flow_job(self) -> None:
        from shared.os_pr_flow import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("PR-flow registration failed on Linux")

    def unregister_pr_flow_job(self, slug: str) -> None:
        from shared.os_pr_flow import _unregister_linux

        _unregister_linux(slug)

    # -- process --

    def process_alive(self, pid: int) -> bool:
        import os

        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def force_kill(self, pid: int) -> None:
        import os

        from shared.platform import SIGKILL

        try:
            os.kill(pid, SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        from shared.pg_tools import PG_BIN_LINUX

        return PG_BIN_LINUX / name


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class WindowsPlatformBackend(PlatformBackend):
    """Windows backend.

    The OS-scheduled jobs (autostart, health probe, package refresh, and
    logs maintenance)
    route through ``shared.os_schtasks``. The data plane is still not wired here
    and stays a deliberate no-op — callers need no ``if IS_WINDOWS`` guards.
    """

    # -- venv --

    def venv_bin_dir_name(self) -> str:
        return "Scripts"

    def venv_python(self) -> str:
        from shared.runtime_interpreter import runtime_python

        return str(runtime_python())

    def venv_launcher(self, name: str, *, root: Path | None = None) -> Path:
        from shared.runtime_interpreter import runtime_venv

        return runtime_venv(checkout=root) / "Scripts" / f"{name}.exe"

    # -- autostart --

    def register_autostart(self) -> None:
        from shared.os_autostart import _register_windows

        reason = _register_windows()
        if reason is not None:
            # Degrade, do not fail the bring-up (the Windows policy; POSIX
            # still fails fast via RuntimeError). A failed registration means
            # this job is absent — no boot autostart — which is a degraded
            # state, but a cluster that is DOWN (converge aborts) is worse,
            # the failure is often transient (win 2026-08-11, task #1196), and
            # every `ava start` retries the registration, so the degradation
            # is loud and self-healing rather than silent and permanent.
            # The reason rides the STDERR line, not just the loguru record.
            # A converge under the updater chain has its stderr captured into the
            # updater log but no loguru sink attached, so the record alone has
            # never reached disk on the fleet's Windows box — the operator saw
            # "registration failed" and nothing else, every update, for months.
            print(  # noqa: T201
                "  ! autostart: schtasks registration failed — continuing without the "
                f"boot autostart job (next `ava start` retries): {reason}",
                file=sys.stderr,
            )
            from loguru import logger

            logger.error("autostart registration failed on Windows: {}", reason)

    def unregister_autostart(self, slug: str) -> None:
        from shared.os_autostart import _unregister_windows

        _unregister_windows(slug)

    # -- cron --

    def register_cron(self, interval_s: int = 300, threshold: int = 3) -> None:
        from shared.os_cron import _register_windows

        reason = _register_windows(interval_s, threshold)
        if reason is not None:
            # Degrade, do not fail the bring-up — see register_autostart for
            # the policy and its rationale (transient failure class, cluster
            # down is worse than unsupervised, every start retries).
            print(  # noqa: T201
                "  ! health probe: schtasks registration failed — continuing without "
                f"the health-probe job (next `ava start` retries): {reason}",
                file=sys.stderr,
            )
            from loguru import logger

            logger.error("cron registration failed on Windows: {}", reason)

    def unregister_cron(self, slug: str) -> None:
        from shared.os_cron import _unregister_windows

        _unregister_windows(slug)

    # -- logs maintenance --

    def register_logs_job(self) -> None:
        from shared.os_logs_job import _register_windows

        reason = _register_windows()
        if reason is not None:
            print(  # noqa: T201
                "  ! logs maintenance: schtasks registration failed — continuing "
                "without daily rotation and retention "
                f"(next `ava start` retries): {reason}",
                file=sys.stderr,
            )
            from loguru import logger

            logger.error("logs-maintenance registration failed on Windows: {}", reason)

    def unregister_logs_job(self, slug: str) -> None:
        from shared.os_logs_job import _unregister_windows

        _unregister_windows(slug)

    # -- packages refresh --

    def register_packages_job(self) -> None:
        from shared.os_packages import _register_windows

        reason = _register_windows()
        if reason is not None:
            # Degrade, do not fail the bring-up — see register_logs_job for the
            # policy and its rationale. Without this job the content-refresh
            # pass only runs manually; the cluster still runs, the warning is
            # loud, and every start retries.
            print(  # noqa: T201
                "  ! packages refresh: schtasks registration failed — continuing "
                "without the recurring refresh pass "
                f"(next `ava start` retries): {reason}",
                file=sys.stderr,
            )
            from loguru import logger

            logger.error("packages-refresh registration failed on Windows: {}", reason)

    def unregister_packages_job(self, slug: str) -> None:
        from shared.os_packages import _unregister_windows

        _unregister_windows(slug)

    # -- pr flow --

    def register_pr_flow_job(self) -> None:
        # Deliberate no-op: this job's command is a script invocation, while
        # schtasks tasks here run the CLI (`python -m cli.main <argv>`) — and
        # the credential gate keeps the job off Windows machines in practice
        # (shared/os_pr_flow.py explains both).
        from loguru import logger

        logger.info("PR-flow sampler job: no Windows registration path; skipped")

    def unregister_pr_flow_job(self, slug: str) -> None:
        # Nothing to remove (see register_pr_flow_job); kept symmetric so the
        # ABC contract holds on every platform.
        from loguru import logger

        logger.debug("PR-flow sampler job: no Windows registration path (slug={})", slug)

    # -- process --

    def process_alive(self, pid: int) -> bool:
        import psutil

        return psutil.pid_exists(pid)

    def force_kill(self, pid: int) -> None:
        import psutil

        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        from shared.pg_tools import PG_BIN_WINDOWS

        candidate = PG_BIN_WINDOWS / f"{name}.exe"
        return candidate if candidate.exists() else None

    # -- capability queries --

    def supports_ava_symlink(self) -> bool:
        return False  # No ~/.local/bin/ava symlink model on Windows

    def supports_shell_rc(self) -> bool:
        return False  # No .zshrc / .bashrc PATH editing on Windows

    def is_posix(self) -> bool:
        return False

    def supports_data_plane(self) -> bool:
        # No native Windows redis exists to drive — not vendored, no Memurai
        # path, and `--daemonize yes` is unimplemented in the Windows forks.
        # This is what scopes Windows to `agent-runner`; the rest of the gap is
        # measured in future/infra/windows-gateway.md.
        return False

    def npm_shell_flag(self) -> bool:
        return True  # npm is npm.cmd — needs shell=True


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_backend: PlatformBackend | None = None


def get_backend() -> PlatformBackend:
    """Return the platform-appropriate ``PlatformBackend`` singleton."""
    global _backend  # noqa: PLW0603
    if _backend is None:
        if IS_WINDOWS:
            _backend = WindowsPlatformBackend()
        elif IS_MACOS:
            _backend = MacPlatformBackend()
        else:
            _backend = LinuxPlatformBackend()
    return _backend
