"""Canonical host-platform detection + the POSIX/Windows portability shims.

The lowest-layer single source of truth for "what OS is this" and for the
handful of POSIX primitives (advisory file locks, the fd-limit raise, the uid,
the kill signals) that have no Windows equivalent and would otherwise crash a
module **at import time** on Windows (``import fcntl`` / ``import resource``) or
**at call time** (``signal.SIGKILL`` / ``signal.SIGHUP`` are undefined on
Windows; ``os.kill(pid, 0)`` actually *terminates* the process there).

Everything in this module is import-safe on every platform: a Windows host must
be able to ``import`` every Ava module without a POSIX-only stdlib module
exploding. The Linux/macOS behaviour is byte-for-byte unchanged — the shims
delegate to the real POSIX primitive on those hosts and only diverge on Windows.

This module sits at the bottom of the import graph (it imports nothing from
``shared``), so any module may import it freely.
"""

from __future__ import annotations

import contextlib
import os
import platform as _osplat
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def launchd_job_label() -> str | None:
    """The launchd label that owns this process, or None outside a LaunchAgent.

    launchd injects ``XPC_SERVICE_NAME`` into the job and its descendants. This
    is per-process scheduler identity, not operator-configurable Ava settings.
    """
    return os.environ.get("XPC_SERVICE_NAME")


# WSL is a Linux kernel whose uname release string carries "microsoft" / "WSL".
# Some host probes (e.g. disk usage) want the Windows host's view, so detect it
# once here rather than re-deriving it from uname at each call site.
def _detect_wsl(uname_release: str) -> bool:
    """True if a Linux uname release string is a WSL kernel (the 'microsoft' /
    'WSL' marker). Factored out so the marker logic is unit-testable without
    monkeypatching uname."""
    release = uname_release.lower()
    return "microsoft" in release or "wsl" in release


IS_WSL = _detect_wsl(_osplat.uname().release)


def venv_python() -> str:
    """Return the platform-appropriate path to the venv Python interpreter.

    On POSIX: ``.venv/bin/python``.
    On Windows: ``.venv\\Scripts\\python.exe`` when the venv exists,
    otherwise ``python`` (system Python).
    """
    if IS_WINDOWS:
        venv_exe = Path(".venv") / "Scripts" / "python.exe"
        if venv_exe.exists():
            return str(venv_exe)
        return "python"
    return ".venv/bin/python"


# --- Kill signals -----------------------------------------------------------
# Windows' `signal` module defines neither SIGKILL nor SIGHUP. Code that names
# them for `os.kill` / handler registration would AttributeError at import or
# call time. On Windows we fall back to SIGTERM, which Python's `os.kill` maps
# to TerminateProcess (an immediate, uncatchable kill — the SIGKILL intent) and
# which the daemons already handle for graceful shutdown (the SIGHUP intent).
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
SIGHUP = getattr(signal, "SIGHUP", signal.SIGTERM)


# --- Child-process window suppression ---------------------------------------
# On Windows, a console-less parent (the agent runner under ConPTY, daemons
# started detached) that spawns a child without a creation flag gets a brand-new
# console window flashed on the interactive desktop for every subprocess call —
# the agent's shell runs, git calls, schtasks invocations all pop a terminal for
# ~1s. CREATE_NO_WINDOW (0x08000000) starts the child with no console at all.
# POSIX has no such flag and no console-window problem; the constant is 0 there,
# so a call site that always passes `creationflags=CREATE_NO_WINDOW` is a no-op
# on every non-Windows host.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def raise_fd_limit(desired: int) -> None:
    """Raise this process's soft RLIMIT_NOFILE toward `desired` (best-effort).

    POSIX only — a process launched from launchd inherits a low (256) fd ceiling.
    Windows has no per-process fd rlimit (the C runtime cap is already high and
    not set this way), so this is a no-op there.
    """
    if IS_WINDOWS:
        return
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = desired if hard == resource.RLIM_INFINITY else min(desired, hard)
    if soft < ceiling:
        resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))


def pty_max() -> int | None:
    """The system-wide pseudo-terminal ceiling, or None where it does not bind.

    macOS caps the number of allocatable PTYs at `kern.tty.ptmx_max` (default
    511, host-wide, NOT per-process). Every agent shell holds one PTY in its
    session host, so this ceiling — not RAM or the fd limit — is the hard wall
    on a dense single box: past it, session spawn fails with
    "openpty: No such file or directory" / "fork failed: Device not
    configured" and the agent never launches. Unlike the fd limit it cannot be
    raised per-process (`sysctl -w kern.tty.ptmx_max=...`, root, host-wide).

    Returns the sysctl value on macOS, or None on Linux/Windows (Linux's
    `kernel.pty.max` defaults to 4096, comfortably above any single-box fleet, so
    it is not a binding constraint here) and on any read failure — callers treat
    None as "no known PTY ceiling to check against".
    """
    if not IS_MACOS:
        return None
    import ctypes

    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(
            b"kern.tty.ptmx_max", ctypes.byref(value), ctypes.byref(size), None, 0
        )
        return value.value if rc == 0 else None
    except (OSError, AttributeError, ValueError):
        return None


class LockTimeoutError(RuntimeError):
    """`file_lock` gave up: another process still held it when the bound expired."""


# How often a bounded wait re-tries the take. Both platforms only offer a
# non-blocking attempt at this granularity, so one poll interval serves both.
_LOCK_POLL_S = 0.05


def _take_nonblocking(fd: int) -> bool:
    """One non-blocking attempt at the exclusive lock on `fd`.

    Only "someone else holds it" is caught. A bare `except OSError` would swallow
    EBADF / EIO — a broken descriptor or a failing filesystem — and re-report it as
    a 30-second wait that ends in `LockTimeoutError`, which names the wrong problem
    and takes 30 seconds to do it. Contention on POSIX is `BlockingIOError`
    (EAGAIN/EWOULDBLOCK); Windows raises `PermissionError` (EACCES) for a range
    another handle holds.
    """
    try:
        if IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]  # Windows-only msvcrt; this branch only runs on Windows
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


@contextlib.contextmanager
def _bounded_file_lock(path: Path, timeout_s: float) -> Generator[None]:
    """`file_lock`'s bounded mode — one poll over the non-blocking take, both
    platforms, so the two branches cannot drift apart."""
    # One bounded path for both platforms: a poll over the non-blocking take.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if IS_WINDOWS and os.fstat(fd).st_size == 0:
            os.write(fd, b"0")  # msvcrt locks a byte RANGE; give it one byte
        deadline = time.monotonic() + timeout_s
        while not _take_nonblocking(fd):
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"could not take {path} within {timeout_s:.0f}s — another "
                    f"process holds it; it is released when that process exits"
                )
            time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if IS_WINDOWS:
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]  # Windows-only msvcrt; this branch only runs on Windows
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout_s: float | None = None) -> Generator[None]:
    """Cross-platform exclusive advisory file lock over `path`.

    POSIX: `fcntl.flock(LOCK_EX)` — the historical behaviour, unchanged.
    Windows: `msvcrt.locking(LK_LOCK)` over one byte, which blocks-with-retry
    until the range is free. Both serialize the host-level registry read-modify-
    write the same way; the lock is released (and the fd closed) on exit.

    `timeout_s` bounds the wait and raises `LockTimeoutError` on expiry, instead
    of blocking indefinitely (`_bounded_file_lock`). **The unbounded default is
    the historical behaviour and is kept for the callers that have it** (the
    cluster registry, `crontab_lock`), but a bound is the better answer wherever
    the holder is a long-lived daemon rather than a short CLI section: a wait
    with no bound is how one wedged holder becomes every writer wedged behind
    it. Expiry raises rather than proceeding — a caller that could not take the
    lock has not established what the lock is for, and writing anyway would be
    the failure the lock exists to prevent, minus the error.

    Either way the OS drops the lock when the holder exits, including on a
    crash, so there is no stale-lock handling and none is wanted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if timeout_s is not None:
        with _bounded_file_lock(path, timeout_s):
            yield
        return
    if IS_WINDOWS:
        import msvcrt

        # msvcrt.locking needs a real, writable fd with at least one byte to
        # lock. LK_LOCK blocks ~10s per call then raises; loop so a long-held
        # lock still serializes instead of spuriously failing.
        f = path.open("a+b")
        try:
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]  # Windows-only msvcrt; this branch only runs on Windows
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]  # Windows-only msvcrt; this branch only runs on Windows
        finally:
            f.close()
    else:
        import fcntl

        with path.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def crontab_lock() -> Generator[None]:
    """Serialize read-modify-write cycles over the user's crontab.

    `crontab -l` → filter → `crontab -` is three steps with no atomicity:
    two co-located clusters (or a gateway restart racing a converge) both
    read the old crontab, and the second writer overwrites the first's
    just-added line — for the watchdog-probe that line is the last line of
    supervision, and losing it silently leaves restarter/ops/browser
    sessions down with nobody watching (audit 2026-08-08 P1).

    The lock file lives OUTSIDE $AVA_HOME (a per-home lock would not
    serialize across clusters sharing one crontab) but inside the user's
    home, so a /tmp sweep cannot unlink it mid-hold. It is advisory —
    every crontab rewrite in the repo must go through this lock.
    """
    with file_lock(Path.home() / ".ava-crontab.lock"):
        yield


def ensure_utf8_stdio() -> None:
    """Force UTF-8 stdout/stderr on Windows (no-op elsewhere).

    The CLI and daemons print status glyphs and occasionally non-ASCII data. A
    Windows console defaults to a legacy code page (cp1252), where printing an
    arrow / checkmark raises UnicodeEncodeError and aborts the command. This:

    - reconfigures *this* process's stdout/stderr to UTF-8 (errors='replace' so a
      stray un-encodable byte degrades to '?' rather than crashing), and
    - sets PYTHONUTF8 / PYTHONIOENCODING in os.environ so every child interpreter
      we spawn (the birth subprocess, the supervisor's daemons + agents, which
      copy os.environ) starts in UTF-8 mode too — their stdout is redirected to a
      log file, which has the same legacy-code-page default without this.

    Idempotent; call once near process start (the CLI entry does).
    """
    if not IS_WINDOWS:
        return
    import os

    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def ensure_line_buffered_stdio() -> None:
    """Make this process's stdout line-buffered even when it is a pipe.

    Python line-buffers stdout only when it is a tty; into a pipe it uses an 8 KiB
    block buffer, so a long-running command's own `print()` lines surface all at
    once when it exits. Every detached orchestration session pipes the CLI into
    `tee` (`{ ava cluster update --local; } 2>&1 | tee -a <log>`), and the CHILD processes
    it spawns — `uv sync`, the `ava start` subprocess — write to that same pipe
    unbuffered. The result is a live log that is not merely late but *misordered*:
    on 2026-07-28 a rollout's log showed `ava start` output and a pin warning with
    no `[ava cluster update]` header, no pin line and no phase markers above them, which
    reads exactly like a rollout that skipped its orchestration. The parent's lines
    all appeared, in the right order, at the END of the file once it exited. It
    cost a false alarm during a live deploy.

    Line buffering is the fix rather than `flush=True` at each call site (there are
    hundreds, and the next one added would silently reintroduce this) or
    `PYTHONUNBUFFERED` in the session env (which would have to be repeated at every
    spawn site, in two shells, and would not help a human piping `ava cluster update` by
    hand). A tty is already line-buffered, so this changes nothing interactively.

    Idempotent; call once near process start (the CLI entry does).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A stream replaced by something without line_buffering support (pytest's
            # capture, a redirect_stdout StringIO) raises; buffering is a nicety, so
            # never let it take down the command.
            with contextlib.suppress(ValueError, OSError, TypeError):
                reconfigure(line_buffering=True)


def primary_disk_path() -> str:
    """The most meaningful filesystem path for this host's disk-usage sampling.

    macOS: the data volume (not the sealed system volume). WSL: the distro's own
    ext4 rootfs (`/`) — NOT the auto-mounted Windows `/mnt/c`, which reflects the
    Windows host's C: drive (often near-full) and has nothing to do with how much
    space this Linux machine is actually using. Windows: the system drive. Any
    other POSIX host: the root filesystem.
    """
    if IS_MACOS:
        return "/System/Volumes/Data"
    if IS_WINDOWS:
        return "C:\\"
    return "/"


def user_systemd_unit_dir() -> Path:
    """The user manager's XDG unit directory, independent of Ava configuration."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    config = Path(configured) if configured else Path.home() / ".config"
    if not config.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path for user systemd units")
    return config / "systemd/user"
