"""POSIX OS abstraction — unifies macOS and Linux platform
differences behind a common interface, following the ``base/sessions/backend.py``
provider pattern.

Module-level ``get_backend()`` returns the platform-appropriate singleton.
Callers use the same ``PlatformBackend`` protocol regardless of platform;
the backend is selected by ``IS_MACOS`` in ``base.native_process.os_platform``.

Design:
  - Abstract methods are the operations that differ by platform.
  - Concrete capability-query methods (``supports_*``) have sensible defaults
    that each subclass can override.
"""

from __future__ import annotations

import abc
from pathlib import Path

from base.native_process.os_platform import IS_LINUX, IS_MACOS

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
        """The name of the virtualenv binary directory (``"bin"``)."""
        ...

    def venv_python(self) -> str:
        """Absolute path to the Python interpreter inside the repo's virtualenv."""
        from base.deploy.release.runtime_interpreter import runtime_venv

        return str(runtime_venv() / self.venv_bin_dir_name() / "python3")

    def venv_launcher(self, name: str, *, root: Path | None = None) -> Path:
        """Absolute path to a console script the virtualenv installs (``"ava"``,
        ``"uv"``, …) — ``.venv/bin/<name>``.

        `root` is the checkout holding the ``.venv`` (defaults to this one's repo
        root); the agent-runner self-update passes the repo it is upgrading.
        """
        from base.deploy.release.runtime_interpreter import runtime_venv

        return runtime_venv(checkout=root) / self.venv_bin_dir_name() / name

    # -- autostart ----------------------------------------------------------

    @abc.abstractmethod
    def register_autostart(self) -> None:
        """Register a boot-time job that runs ``ava start`` on reboot.

        macOS: launchd RunAtLoad LaunchAgent plist.
        Linux: the enabled distro-level systemd unit (``base.host.system.boot_unit``).

        Reached only through ``base.host.system.autostart.register_autostart``, which
        applies the ``os_jobs_enabled()`` gate — call that, not this.

        Idempotent. Raises ``RuntimeError`` on registration failure.
        """
        ...

    @abc.abstractmethod
    def unregister_autostart(self, home: Path) -> None:
        """Remove the boot-time autostart job bound to the explicit ``home``. Safe when none is registered.

        The home is passed in rather than re-derived from ``$AVA_HOME``: the only
        caller that removes *another* cluster's jobs (``ava cluster destroy``)
        runs inside a process whose own settings were frozen at import.
        """
        ...

    # -- cron ---------------------------------------------------------------

    @abc.abstractmethod
    def register_cron(self, interval_s: int = 300) -> None:
        """Register the periodic OS cron job for the cluster health probe.

        macOS: launchd StartInterval LaunchAgent plist.
        Linux: user crontab entry.

        Reached only through ``base.host.system.cron.register_os_cron``, which applies
        the ``os_jobs_enabled()`` gate — call that, not this.

        Idempotent — re-running updates the interval. Raises
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

        Reached only through ``base.host.system.pr_flow_job.register_pr_flow_job``, which
        applies the ``os_jobs_enabled()`` gate plus the credential gate (gh +
        Trunk token + production home) — call that, not this.

        Idempotent — re-running replaces the definition. Raises ``RuntimeError``
        on registration failure.
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
        """
        ...

    @abc.abstractmethod
    def force_kill(self, pid: int) -> None:
        """Force-terminate ``pid``. A dead/absent pid is a silent no-op.

        POSIX: ``os.kill(pid, SIGKILL)``.
        """
        ...

    # -- PostgreSQL ---------------------------------------------------------

    @abc.abstractmethod
    def pg_binary_path(self, name: str) -> Path | None:
        """Resolve a PostgreSQL binary name to a full path, or ``None`` when
        the host has no known installation.

        The vendored relocatable Postgres (``base.cluster.dataplane.runtime_binaries``) is
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
        """Whether ``npm`` commands need ``shell=True`` (False on supported hosts)."""
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
        from base.host.system.autostart import _register_macos

        rc = _register_macos()
        if rc != 0:
            raise RuntimeError("autostart registration failed on macOS")

    def unregister_autostart(self, home: Path) -> None:
        from base.cluster import home_slug
        from base.host.system.autostart import _unregister_macos

        _unregister_macos(home_slug(home))

    # -- cron --

    def register_cron(self, interval_s: int = 300) -> None:
        from base.host.system.cron import _register_macos

        rc = _register_macos(interval_s)
        if rc != 0:
            raise RuntimeError("cron registration failed on macOS")

    def unregister_cron(self, slug: str) -> None:
        from base.host.system.cron import _unregister_macos

        _unregister_macos(slug)

    # -- logs maintenance --

    def register_logs_job(self) -> None:
        from base.host.system.logs_job import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("logs-maintenance registration failed on macOS")

    def unregister_logs_job(self, slug: str) -> None:
        from base.host.system.logs_job import _unregister_macos

        _unregister_macos(slug)

    # -- packages refresh --

    def register_packages_job(self) -> None:
        from base.host.system.packages_job import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("packages-refresh registration failed on macOS")

    def unregister_packages_job(self, slug: str) -> None:
        from base.host.system.packages_job import _unregister_macos

        _unregister_macos(slug)

    # -- pr flow --

    def register_pr_flow_job(self) -> None:
        from base.host.system.pr_flow_job import _register_macos

        if _register_macos() != 0:
            raise RuntimeError("PR-flow registration failed on macOS")

    def unregister_pr_flow_job(self, slug: str) -> None:
        from base.host.system.pr_flow_job import _unregister_macos

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

        from base.native_process.os_platform import SIGKILL

        try:
            os.kill(pid, SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        from base.cluster.dataplane.pg_tools import brew_prefix

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
        from base.host.system.boot_unit import install

        install()

    def unregister_autostart(self, home: Path) -> None:
        from base.host.system.boot_unit import uninstall

        uninstall(home)

    # -- cron --

    def register_cron(self, interval_s: int = 300) -> None:
        from base.host.system.cron import _register_linux

        rc = _register_linux(interval_s)
        if rc != 0:
            raise RuntimeError("cron registration failed on Linux")

    def unregister_cron(self, slug: str) -> None:
        from base.host.system.cron import _unregister_linux

        _unregister_linux(slug)

    # -- logs maintenance --

    def register_logs_job(self) -> None:
        from base.host.system.logs_job import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("logs-maintenance registration failed on Linux")

    def unregister_logs_job(self, slug: str) -> None:
        from base.host.system.logs_job import _unregister_linux

        _unregister_linux(slug)

    # -- packages refresh --

    def register_packages_job(self) -> None:
        from base.host.system.packages_job import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("packages-refresh registration failed on Linux")

    def unregister_packages_job(self, slug: str) -> None:
        from base.host.system.packages_job import _unregister_linux

        _unregister_linux(slug)

    # -- pr flow --

    def register_pr_flow_job(self) -> None:
        from base.host.system.pr_flow_job import _register_linux

        if _register_linux() != 0:
            raise RuntimeError("PR-flow registration failed on Linux")

    def unregister_pr_flow_job(self, slug: str) -> None:
        from base.host.system.pr_flow_job import _unregister_linux

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

        from base.native_process.os_platform import SIGKILL

        try:
            os.kill(pid, SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        from base.cluster.dataplane.pg_tools import PG_BIN_LINUX

        return PG_BIN_LINUX / name


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_backend: PlatformBackend | None = None


def get_backend() -> PlatformBackend:
    """Return the platform-appropriate ``PlatformBackend`` singleton."""
    global _backend  # noqa: PLW0603
    if _backend is None:
        if not (IS_MACOS or IS_LINUX):
            raise RuntimeError("unsupported host platform for OS jobs")
        _backend = MacPlatformBackend() if IS_MACOS else LinuxPlatformBackend()
    return _backend
