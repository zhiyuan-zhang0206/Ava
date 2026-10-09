"""Host-platform facts and POSIX process primitives below the import graph.

The Windows flag and subprocess constants remain as compatibility symbols for
protected release and cluster modules while native Windows runtime is retired.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path


def is_windows() -> bool:
    """Whether this process runs on Windows, queried directly from Python."""
    return sys.platform == "win32"


def is_macos() -> bool:
    """Whether this process runs on macOS, queried directly from Python."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """Whether this process runs on Linux, queried directly from Python."""
    return sys.platform.startswith("linux")


def launchd_job_label() -> str | None:
    """The launchd label naming this process, or None outside a LaunchAgent.

    launchd sets ``XPC_SERVICE_NAME`` only for the job's DIRECT child — the job
    process itself. Every exec'd descendant reads ``"0"`` on current macOS
    (2026-09-17, three LaunchAgent shapes incl. the bash -> python chain a
    converge runs under), so this is a hint, not proof of ownership:
    destructive converges must confirm against the live process tree with
    :func:`descends_from_launchd_job`. Per-process scheduler identity, not
    operator-configurable Ava settings.
    """
    return os.environ.get("XPC_SERVICE_NAME")


def _launchd_print(label: str) -> subprocess.CompletedProcess[str] | None:
    """One ``launchctl print`` resolution for a gui-domain label; None off macOS.

    Shared by :func:`launchd_job_loaded` (verdict) and
    :func:`descends_from_launchd_job` (live pid): the dump on success is the
    caller's to parse; off macOS there is nothing to ask."""
    if not is_macos():
        return None
    return subprocess.run(  # noqa: S603
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )


def launchd_job_loaded(label: str) -> bool:
    """Whether launchd currently holds a job under ``label`` (macOS; False elsewhere).

    ``launchctl print`` exits non-zero for a label the domain does not know —
    the only question asked here; the large dump it writes on success is
    ignored. Mirrors the gate layer's ``_job_loaded`` checks (same verdict) as
    the shared surface they can converge onto — no in-tree production consumer
    yet; the reload guards consume :func:`descends_from_launchd_job`, and the
    tests pin this verdict.
    """
    result = _launchd_print(label)
    return result is not None and result.returncode == 0


def descends_from_launchd_job(label: str) -> bool:
    """True when this process runs inside the live process tree of launchd job ``label``.

    The ownership boundary behind the self-reload guards (``base/host/system/cron``,
    ``base/os_watchdog_probe``): ``launchctl bootout`` terminates the job's
    whole process tree, so a converge running beneath the job it is about to
    replace would kill its own recovery. The inherited ``XPC_SERVICE_NAME``
    cannot prove ownership — only the job's direct child reads the label
    while descendants read ``"0"`` (docs/postmortems/0008) — so ask the scheduler
    for the job's current pid and walk this process's ancestry instead.

    False off macOS, when the job is not loaded/not running (it owns no live
    process then), and whenever an ancestor cannot be read: callers proceed
    with a replacement unless ownership is PROVEN.
    """
    result = _launchd_print(label)
    if result is None or result.returncode != 0:
        return False
    match = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout)
    if match is None:
        return False  # loaded but not running — it owns no live process
    job_pid = int(match.group(1))
    pid = os.getpid()
    while pid > 1:  # bounded by reaching launchd (pid 1)
        if pid == job_pid:
            return True
        parent = _parent_pid(pid)
        if parent is None or parent == pid:
            return False
        pid = parent
    return False


def _parent_pid(pid: int) -> int | None:
    """The parent pid of ``pid``, or None when ps cannot answer."""
    result = subprocess.run(  # noqa: S603
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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

    A process launched from launchd can inherit a low (256) fd ceiling.
    """
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
    if not is_macos():
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


# How often a bounded wait retries a non-blocking lock attempt.
_LOCK_POLL_S = 0.05


def _take_nonblocking(fd: int) -> bool:
    """One non-blocking attempt at the exclusive lock on `fd`.

    Only contention is caught; broken descriptors and filesystems remain loud.
    """
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


@contextlib.contextmanager
def _bounded_file_lock(path: Path, timeout_s: float) -> Generator[None]:
    """`file_lock`'s bounded mode — poll non-blocking take until deadline."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while not _take_nonblocking(fd):
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"could not take {path} within {timeout_s:g}s — another "
                    f"process holds it; it is released when that process exits"
                )
            time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout_s: float | None = None) -> Generator[None]:
    """POSIX exclusive advisory file lock over `path`.

    `timeout_s` bounds the wait and raises `LockTimeoutError` on expiry; the
    unbounded default keeps historical caller behavior. The OS drops the lock
    on process exit, including a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if timeout_s is not None:
        with _bounded_file_lock(path, timeout_s):
            yield
        return
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
    supervision, and losing it silently leaves agent-host/ops/browser
    sessions down with nobody watching (audit 2026-08-08 P1).

    The lock file lives OUTSIDE $AVA_HOME (a per-home lock would not
    serialize across clusters sharing one crontab) but inside the user's
    home, so a /tmp sweep cannot unlink it mid-hold. It is advisory —
    every crontab rewrite in the repo must go through this lock.
    """
    with file_lock(Path.home() / ".ava-crontab.lock"):
        yield


def ensure_line_buffered_stdio() -> None:
    """Make this process's stdout line-buffered even when it is a pipe.

    Python line-buffers stdout only when it is a tty; into a pipe it uses an 8 KiB
    block buffer, so a long-running command's own `print()` lines surface all at
    once when it exits. Every detached orchestration session pipes the CLI into
    `tee` (`{ <update command>; } 2>&1 | tee -a <log>`), and the CHILD processes
    it spawns — `uv sync`, the `ava start` subprocess — write to that same pipe
    unbuffered. The result is a live log that is not merely late but *misordered*:
    on 2026-07-28 a rollout's log showed `ava start` output and a pin warning with
    no `[update]` header, no pin line and no phase markers above them, which
    reads exactly like a rollout that skipped its orchestration. The parent's lines
    all appeared, in the right order, at the END of the file once it exited. It
    cost a false alarm during a live deploy.

    Line buffering is the fix rather than `flush=True` at each call site (there are
    hundreds, and the next one added would silently reintroduce this) or
    `PYTHONUNBUFFERED` in the session env (which would have to be repeated at every
    spawn site, in two shells, and would not help a human piping the fleet update script by
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
    if is_macos():
        return "/System/Volumes/Data"
    return "/"
