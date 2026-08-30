"""subprocess.Popen + wait-for-port helpers for e2e fixtures."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Servers currently running under `managed_proc`, label -> (proc, log_path).
# `pytest_runtest_makereport` in conftest.py reads this on failure so a server
# that should have been up but wasn't (issue #213) leaves its exit code and log
# tail IN the failing report instead of only in an artifact nobody opens.
_LIVE_SERVERS: dict[str, tuple[subprocess.Popen[str], str | None]] = {}


def proc_log_tail(log_path: str | None, n: int = 40) -> str:
    """Last `n` lines of a managed process's merged log (or a short reason why
    there is no tail) — the evidence a dead server leaves behind."""
    if log_path is None:
        return "(no log_path; stdout was inherited by pytest)"
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(log unreadable: {e!r})"
    if not lines:
        return "(log is empty)"
    return "\n".join(lines[-n:])


def dead_server_evidence() -> str:
    """For every registered server that exited while its fixture was still
    active, the exit code + log tail — appended to failing test reports."""
    parts: list[str] = []
    for label, (proc, log_path) in sorted(_LIVE_SERVERS.items()):
        code = proc.poll()
        if code is None:
            continue
        parts.append(
            f"[e2e] server '{label}' (pid {proc.pid}) was dead at failure time — "
            f"exit code {code}; log tail:\n{proc_log_tail(log_path)}"
        )
    return "\n\n".join(parts)


def wait_for_port(host: str, port: int, timeout: float = 30.0, *, label: str) -> None:
    """Poll TCP connect (host, port) until success or timeout; raises RuntimeError otherwise."""
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(
        f"Waiting for {label} ({host}:{port}) timed out after {timeout}s; last error: {last_err!r}"
    )


_SIGKILL_GRACE_SEC = 2.0


@contextmanager
def managed_proc(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    label: str,
    stop_signal: int = signal.SIGTERM,
    stop_timeout: float = 10.0,
    log_path: str | None = None,
) -> Generator[subprocess.Popen[str]]:
    """Start subprocess + clean teardown kill.

    Uses `start_new_session=True` to make the child a process group leader;
    on teardown, sends SIGTERM to the whole group -- next dev / uvicorn both fork
    children; a single SIGTERM to the leader is not enough, need group-level kill.

    If `log_path` is given, merges stdout+stderr into that file -- lets the fixture
    dump child process logs on failure. When None, stdout/stderr inherit (go to
    pytest terminal).

    Teardown sequence: SIGTERM -> wait(stop_timeout) -> if not reaped, SIGKILL ->
    wait again (_SIGKILL_GRACE_SEC, default 2s). After SIGKILL, normal reaping is
    sub-second; short grace prevents unbounded waiting on D state hangs. Still
    timed out -> raise RuntimeError.
    """
    log_file = open(log_path, "w") if log_path is not None else None  # noqa: SIM115, PTH123 -- held for process lifetime, finally close
    stdout: int | object = log_file if log_file is not None else None
    stderr: int | object = subprocess.STDOUT if log_file is not None else None
    try:
        proc = subprocess.Popen(  # noqa: S603 -- cmd is a fixture-hardcoded constant
            cmd,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
    except Exception:
        # Popen failure (ENOENT etc.) must immediately close log_file to prevent
        # leak -- the finally: close below will not run because yield was never entered.
        if log_file is not None:
            log_file.close()
        raise
    _LIVE_SERVERS[label] = (proc, log_path)
    try:
        yield proc
    finally:
        _LIVE_SERVERS.pop(label, None)
        try:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, stop_signal)
                try:
                    proc.wait(timeout=stop_timeout)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    # Short grace after SIGKILL (default 2s) -- SIGKILL cannot be
                    # ignored; reaping is sub-second. Still timed out means D state /
                    # NFS / zombie wedged; raise diagnostic.
                    try:
                        proc.wait(timeout=_SIGKILL_GRACE_SEC)
                    except subprocess.TimeoutExpired as e:
                        raise RuntimeError(
                            f"[{label}] pid={proc.pid} {_SIGKILL_GRACE_SEC}s after SIGKILL "
                            f"still not reaped (uninterruptible sleep / NFS / zombie); "
                            f"see {log_path or '(stdout inherited)'}"
                        ) from e
        finally:
            if log_file is not None:
                log_file.close()


# ---- stale-run residue reaper --------------------------------------------
#
# Every child `managed_proc` starts gets its OWN session (`start_new_session`),
# so when a pytest session dies without running fixture teardowns — the agent's
# shell killed, a wall-clock limit, a hard stop — the gateway / ops / restarter
# / agent / frontend / playwright processes it launched keep running as
# session leaders with no parent left to notice them. Observed 2026-08-30: a
# dead run left the gateway, the ops daemon, two agents and the Next.js
# frontend running for 12+ hours — and chained local runs accumulate exactly
# one stack per dead session (the class 5115 measured before the host session
# guard flagged it).
#
# Fixture teardowns CANNOT fix that class of leak — they never run after a
# kill -9. What fixes it is a reaper the NEXT session runs before it spawns
# anything: identify e2e processes by their AVA_HOME (`tmp/ava_e2e_home_<pid>_<ts>`
# under the checkout — every gateway/ops/agent/daemon/browser process inherits
# it) or, for the session-scoped frontend (its env snapshot predates the e2e
# env layering), by its build dir cwd (`ui/web/.builds/build-<pid>_<ts>`), and
# kill the ones whose owning pytest pid is gone. A live concurrent run (other
# agent's session) is preserved by construction: its owner pid is alive.


# A `ps eww ax` row: `PID TT STAT TIME COMMAND [ENV...]`. The command part is
# argv up to the first env-shaped token (`NAME=value`); no fixture argv is
# env-shaped, so the split is unambiguous for everything this suite starts.
_PS_ENV_ROW_RE = re.compile(r"^\s*(\d+)\s+\S+\s+\S+\s+\S+\s+(.*)$")
_ENV_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*=")

# The e2e run id embedded in every throwaway home / build dir:
# `ava_e2e_home_<pytest-pid>_<microsecond-ts>` and `build-<pytest-pid>_<ts>`.
_E2E_HOME_RUN_RE = re.compile(r"ava_e2e_home_(\d+)_(\d+)")
_E2E_BUILD_RUN_RE = re.compile(r"/ui/web/\.builds/build-(\d+)_(\d+)")

_FRONTEND_HINTS = ("next", "npm")
_REAP_GRACE_SEC = 2.0


@dataclass(frozen=True)
class E2EProcess:
    """One live process owned by an e2e run, with the run id that owns it."""

    pid: int
    pgid: int
    cmdline: str
    run: tuple[int, int]  # (owning pytest pid, suffix microsecond timestamp)


def _parse_run_id(text: str, pattern: re.Pattern[str]) -> tuple[int, int] | None:
    m = pattern.search(text)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def _split_cmdline_env(rest: str) -> tuple[str, str]:
    tokens = rest.split()
    for i, tok in enumerate(tokens):
        if _ENV_TOKEN_RE.match(tok):
            return " ".join(tokens[:i]), " ".join(tokens[i:])
    return rest, ""


def _ps_rows_with_env() -> list[tuple[int, str, str]]:
    """(pid, cmdline, env) for every process — `ps eww ax` on macOS (env
    inline), /proc on Linux. Windows returns nothing: a no-op there is fine,
    e2e on Windows is not a supported shape (POSIX-only gateway)."""
    if sys.platform == "win32":
        return []
    if sys.platform != "darwin":
        rows: list[tuple[int, str, str]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                cmdline = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", errors="replace")
                )
                env = (
                    (entry / "environ")
                    .read_bytes()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", errors="replace")
                )
            except OSError:
                continue
            rows.append((pid, cmdline.strip(), env.strip()))
        return rows
    try:
        out = subprocess.run(  # argv is the static "ps eww ax"
            ["ps", "axeww"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        m = _PS_ENV_ROW_RE.match(line)
        if m is None:
            continue
        pid = int(m.group(1))
        cmdline, env = _split_cmdline_env(m.group(2))
        rows.append((pid, cmdline, env))
    return rows


def _cwd_of(pid: int) -> str | None:
    if sys.platform == "win32":
        return None
    if sys.platform != "darwin":
        try:
            # Path.readlink() returns a Path — coerce to str: callers
            # (scan → _parse_run_id) expect the same shape as the macOS branch.
            return str(Path(f"/proc/{pid}/cwd").readlink())
        except OSError:
            return None
    try:
        out = subprocess.run(  # noqa: S603 -- argv is the static "lsof -a -d cwd -p"
            ["lsof", "-a", "-d", "cwd", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_lsof_cwd(out)


def _parse_lsof_cwd(out: str) -> str | None:
    """The NAME column of a `lsof -d cwd` line: `COMMAND PID USER FD TYPE
    DEVICE SIZE/OFF NODE NAME` (NAME may contain spaces, so split with
    maxsplit and keep the tail)."""
    for line in out.splitlines():
        fields = line.split(None, 8)
        if len(fields) >= 9 and fields[3] == "cwd":
            return fields[8]
    return None


def _looks_like_frontend(cmdline: str) -> bool:
    return any(hint in cmdline for hint in _FRONTEND_HINTS)


def scan_e2e_processes() -> list[E2EProcess]:
    """Every live process this checkout's e2e suite owns, tagged with its run id.

    Two identification paths, because the session-scoped frontend does not
    carry the e2e env (its env snapshot is taken before `_e2e_process_env`
    lays the e2e values): most processes carry `AVA_HOME=.../ava_e2e_home_...`;
    the frontend is found by its `.builds/build-<pid>_<ts>` cwd instead.
    """
    procs: list[E2EProcess] = []
    for pid, cmdline, env in _ps_rows_with_env():
        run = _parse_run_id(env, _E2E_HOME_RUN_RE)
        if run is None and _looks_like_frontend(cmdline):
            run = _parse_run_id(_cwd_of(pid) or "", _E2E_BUILD_RUN_RE)
        if run is None:
            continue
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            continue
        procs.append(E2EProcess(pid=pid, pgid=pgid, cmdline=cmdline, run=run))
    return procs


_LIVE_RUN_HINTS = ("pytest", "xdist")


def _ps_command_of(pid: int) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 -- argv is the static "ps -o command= -p"
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out or None


def _owner_live(owner_pid: int) -> bool:
    """The owning run is really still alive.

    `os.kill(pid, 0)` alone is not enough: a dead pytest's pid can be recycled
    by an unrelated process within days, which would give its run's residue a
    live-looking owner and hide it forever. An e2e run's owner is a pytest
    process (serial) or an xdist worker (the `-n` shape), so its command line
    is the second half of the check.
    """
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = _ps_command_of(owner_pid)
    return cmdline is not None and any(hint in cmdline for hint in _LIVE_RUN_HINTS)


def _identity_holds(pid: int, cmdline: str) -> bool:
    """Re-verify a matched process right before signalling it (TOCTOU guard).

    Between scanning and signalling, a pid can be recycled by an unrelated
    process; a killpg / os.kill would then hit a foreign process. The command
    line is the identity token — whitespace-normalized, because `ps eww` and
    `ps -o command=` may differ only in padding."""
    current = _ps_command_of(pid)
    if current is None:
        return False
    return " ".join(current.split()) == " ".join(cmdline.split())


def _sweep_targets(
    procs: list[E2EProcess],
    *,
    own_pid: int,
    own_pgrp: int,
    include_own: bool,
    owner_live: Callable[[int], bool],
) -> tuple[set[int], set[int], set[int]]:
    """Partition e2e processes into (groups, singles, dead owners) to kill.

    A process is a target when its run id's owner is really gone (killed
    session, crash) — a run whose owner is still a live pytest/xdist worker
    (a concurrent e2e session on this host, which the suite supports) is never
    touched. With `include_own=True` the current session's own processes are
    targets too. A group is killable wholesale ONLY when the matched
    process IS its group leader (uv/npm/detached server trees, whose members
    are all its descendants), and the kill is re-verified right before it
    fires; every non-leader (playwright workers, agents parented to a dead
    launcher, ordinary group members) stays in `singles`, each verified
    individually before its signal — a group may hold processes the matched
    one merely happened to share a session with, and our own pgrp is never
    killable at all.
    """
    groups: set[int] = set()
    owners: set[int] = set()
    targets: list[E2EProcess] = []
    for proc in procs:
        owner_pid, _ = proc.run
        if owner_pid == own_pid:
            if not include_own:
                continue
        elif owner_live(owner_pid):
            continue
        if proc.pid == own_pid:
            continue
        owners.add(owner_pid)
        targets.append(proc)
        if proc.pgid == proc.pid and proc.pgid != own_pgrp:
            groups.add(proc.pgid)
    singles = {proc.pid for proc in targets if proc.pgid != proc.pid}
    return groups, singles, owners


def sweep_stale_e2e_processes(*, include_own: bool = False) -> int:
    """Kill e2e processes whose owning pytest run is gone; return the count.

    Run at session start (before this run spawns anything) and again at the
    e2e package teardown with `include_own=True`, so a process that escaped
    its fixture teardown during THIS session is also reaped.
    """
    procs = scan_e2e_processes()
    groups, singles, owners = _sweep_targets(
        procs,
        own_pid=os.getpid(),
        own_pgrp=os.getpgrp(),
        include_own=include_own,
        owner_live=_owner_live,
    )
    if not groups and not singles:
        return 0
    cmdline_by_pid = {proc.pid: proc.cmdline for proc in procs}
    # Every signal lands only after the process's identity is re-verified — a
    # killed pytest's pids can be recycled, and an unverified killpg/os.kill
    # would hit whatever now holds them. A leader whose identity no longer
    # holds is skipped; its members are still in `singles` and verified there.
    for pgid in groups:
        leader_cmdline = cmdline_by_pid.get(pgid)
        if leader_cmdline is None or not _identity_holds(pgid, leader_cmdline):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    for pid in singles:
        if not _identity_holds(pid, cmdline_by_pid.get(pid, "")):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    print(  # noqa: T201 -- must reach the terminal; loguru output is captured
        f"\nE2E RESIDUE: reaped {len(groups) + len(singles)} process(es) left by "
        f"dead pytest run(s) {sorted(owners)} (gateway/agent/daemon/frontend of a "
        "session that never ran its fixture teardowns)",
        file=sys.stderr,
    )
    time.sleep(_REAP_GRACE_SEC)
    for pgid in groups:
        if not _identity_holds(pgid, cmdline_by_pid.get(pgid, "")):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    for pid in singles:
        if not _identity_holds(pid, cmdline_by_pid.get(pid, "")):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    time.sleep(1.0)
    return len(groups) + len(singles)
