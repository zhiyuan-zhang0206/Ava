"""POSIX OS abstraction — unifies macOS and Linux platform
differences behind a common interface, following the ``base/sessions/backend.py``
provider pattern.

Module-level ``get_backend()`` returns the platform-appropriate backend
(stateless, built per call).
Callers use the same ``PlatformBackend`` protocol regardless of platform;
the backend is selected by ``is_macos()`` in ``base.native_process.os_platform``.

Design:
  - Abstract methods are the operations that differ by platform.
  - Concrete capability-query methods (``supports_*``) have sensible defaults
    that each subclass can override.
"""

from __future__ import annotations

import abc
from pathlib import Path

from base.host.brew_pin import brew_prefix
from base.native_process.os_platform import is_linux, is_macos

PG_BIN_LINUX = Path("/usr/lib/postgresql/17/bin")

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
        from base.host.runtime_interpreter import runtime_venv

        return str(runtime_venv() / self.venv_bin_dir_name() / "python3")

    def venv_launcher(self, name: str, *, root: Path | None = None) -> Path:
        """Absolute path to a console script the virtualenv installs (``"ava"``,
        ``"uv"``, …) — ``.venv/bin/<name>``.

        `root` is the checkout holding the ``.venv`` (defaults to this one's repo
        root); the agent-runner self-update passes the repo it is upgrading.
        """
        from base.host.runtime_interpreter import runtime_venv

        return runtime_venv(checkout=root) / self.venv_bin_dir_name() / name

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


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


class MacPlatformBackend(PlatformBackend):
    """macOS backend (darwin)."""

    # -- venv --

    def venv_bin_dir_name(self) -> str:
        return "bin"

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
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        return brew_prefix("postgresql@17") / "bin" / name


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


class LinuxPlatformBackend(PlatformBackend):
    """Linux backend."""

    # -- venv --

    def venv_bin_dir_name(self) -> str:
        return "bin"

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
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    # -- PostgreSQL --

    def pg_binary_path(self, name: str) -> Path | None:
        return PG_BIN_LINUX / name


# ---------------------------------------------------------------------------
# Platform selection
# ---------------------------------------------------------------------------


def get_backend() -> PlatformBackend:
    """Return the platform-appropriate ``PlatformBackend`` (stateless: built per call)."""
    if not (is_macos() or is_linux()):
        raise RuntimeError("unsupported host platform for OS jobs")
    return MacPlatformBackend() if is_macos() else LinuxPlatformBackend()
