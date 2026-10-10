"""subprocess.Popen + wait-for-port helpers for e2e fixtures."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

from base.sessions.posixproc import process_group_has_live_members
from base.sessions.pty import client as pty_client

__all__ = [
    "E2EProcess",
    "ManagedServer",
    "ProcessInspection",
    "ProcessObservation",
    "ResidueSweepPlan",
    "dead_server_evidence",
    "fixture_entrypoint",
    "kill_group_if_alive",
    "kill_group_or_prove_already_gone",
    "listener_evidence",
    "managed_proc",
    "proc_log_tail",
    "pty_sessions_proc",
    "registered_server",
    "require_native_listener",
    "scan_e2e_processes",
    "sweep_stale_e2e_processes",
    "wait_for_port",
]


def listener_evidence(port: int, phase: str) -> dict[str, object]:
    """Native identities at one port; unreadable visibility is not absence proof."""
    rows: list[dict[str, object]] = []
    try:
        for connection in psutil.net_connections(kind="tcp"):
            if not connection.laddr or connection.laddr.port != port:
                continue
            birth: float | None = None
            if connection.pid is not None:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    birth = psutil.Process(connection.pid).create_time()
            rows.append({"pid": connection.pid, "birth": birth, "status": connection.status})
    except psutil.AccessDenied:
        return {"phase": phase, "port": port, "visibility": "unknown", "connections": rows}
    return {"phase": phase, "port": port, "visibility": "observed", "connections": rows}


def require_native_listener(pid: int, birth: float, port: int) -> None:
    """HTTP success must belong to the new exact fixture process, not its predecessor."""
    process = psutil.Process(pid)
    if process.create_time() != birth or not any(
        row.laddr and row.laddr.port == port and row.status == psutil.CONN_LISTEN
        for row in process.net_connections(kind="tcp")
    ):
        raise RuntimeError("new gateway fixture does not own the expected listener")


# Servers currently running under `managed_proc`, label -> (proc, log_path).
# `pytest_runtest_makereport` in conftest.py reads this on failure so a server
# that should have been up but wasn't (issue #213) leaves its exit code and log
# tail IN the failing report instead of only in an artifact nobody opens.
_LIVE_SERVERS: dict[str, tuple[subprocess.Popen[str], str | None]] = {}


@dataclass(frozen=True)
class ManagedServer:
    """A registered fixture handle; the registry stays with its process owner."""

    process: subprocess.Popen[str]
    log_path: str | None


def registered_server(label: str) -> ManagedServer:
    """Query one active fixture without granting mutation of the owner registry."""
    process, log_path = _LIVE_SERVERS[label]
    return ManagedServer(process, log_path)


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


def kill_group_or_prove_already_gone(
    proc: subprocess.Popen[bytes] | subprocess.Popen[str], exc: OSError
) -> None:
    """After an owned group signal refusal, prove its leader and members are gone.

    Callers create a new session (pgid == pid). A leader can exit between the
    liveness query and killpg; macOS can return EPERM for a zombie-only group.
    Require both a bounded leader wait and absence of live group members;
    an unexplained refusal remains a real teardown failure.
    """
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"killpg({proc.pid}, ...) raised {exc!r} but the leader never exited"
        ) from exc
    if process_group_has_live_members(proc.pid):
        raise AssertionError(
            f"killpg({proc.pid}, ...) raised {exc!r} but its process group still has live members"
        ) from exc


def kill_group_if_alive(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    """SIGKILL an owned live group, proving it gone if the signal is refused."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError) as exc:
            kill_group_or_prove_already_gone(proc, exc)


_SIGKILL_GRACE_SEC = 2.0


def _write_cleanup_receipt(path: Path | None, event: str, **fields: object) -> None:
    if path is None:
        return
    # Diagnostic I/O must not replace the existing cleanup result or exception.
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as output:
            output.write(
                json.dumps(
                    {"event": event, "sender": os.getpid(), "time_ns": time.time_ns(), **fields}
                )
                + "\n"
            )


def _signal_cleanup(
    path: Path | None,
    event: str,
    target: int,
    stop_signal: int,
    send: Callable[[int, int], None],
    **fields: object,
) -> None:
    fields = {"target": target, "signal": stop_signal, **fields}
    _write_cleanup_receipt(path, event, outcome="attempt", **fields)
    try:
        send(target, stop_signal)
    except Exception as exc:
        _write_cleanup_receipt(path, event, outcome=type(exc).__name__, **fields)
        raise
    _write_cleanup_receipt(path, event, outcome="sent", **fields)


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
    pass_fds: tuple[int, ...] = (),
    receipt_path: Path | None = None,
) -> Generator[subprocess.Popen[str]]:
    """Own a new-session process group and tear it down with bounded escalation.

    Send the requested stop signal, wait stop_timeout, then SIGKILL and wait
    _SIGKILL_GRACE_SEC; a survivor raises RuntimeError. Append stdout/stderr to
    log_path when provided, otherwise inherit them. Optional cleanup receipts
    identify the exact owner and signal operation without changing its result.
    """
    log_file = open(log_path, "a") if log_path is not None else None  # noqa: SIM115, PTH123 -- held for process lifetime, finally close
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
            pass_fds=pass_fds,
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
                try:
                    _signal_cleanup(
                        receipt_path,
                        "managed_signal",
                        proc.pid,
                        stop_signal,
                        os.killpg,
                        owner=os.getpid(),
                        label=label,
                        kind="group",
                    )
                except (PermissionError, ProcessLookupError) as exc:
                    kill_group_or_prove_already_gone(proc, exc)
                try:
                    proc.wait(timeout=stop_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        _signal_cleanup(
                            receipt_path,
                            "managed_signal",
                            proc.pid,
                            signal.SIGKILL,
                            os.killpg,
                            owner=os.getpid(),
                            label=label,
                            kind="group",
                        )
                    except (PermissionError, ProcessLookupError) as exc:
                        kill_group_or_prove_already_gone(proc, exc)
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
class ProcessObservation:
    """One native process table row, before E2E ownership classification."""

    pid: int
    cmdline: str
    env: str

    @classmethod
    def from_ps_row(cls, line: str) -> ProcessObservation | None:
        """Decode a complete macOS process row, retaining command/environment boundaries."""
        match = _PS_ENV_ROW_RE.match(line)
        if match is None:
            return None
        command, env = _split_cmdline_env(match.group(2))
        return cls(int(match.group(1)), command, env)


@dataclass(frozen=True)
class E2EProcess:
    """One live process owned by an e2e run, with the run id that owns it."""

    pid: int
    pgid: int
    cmdline: str
    run: tuple[int, int]  # (owning pytest pid, suffix microsecond timestamp)

    @classmethod
    def from_observation(
        cls, observation: ProcessObservation, *, pgid: int, cwd: str | None = None
    ) -> E2EProcess | None:
        """Classify environment-marked children and frontend build-directory observations."""
        run = _parse_run_id(observation.env, _E2E_HOME_RUN_RE)
        if run is None and _looks_like_frontend(observation.cmdline):
            run = _parse_run_id(cwd or "", _E2E_BUILD_RUN_RE)
        if run is None:
            return None
        return cls(observation.pid, pgid, observation.cmdline, run)


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
        observation = ProcessObservation.from_ps_row(line)
        if observation is not None:
            rows.append((observation.pid, observation.cmdline, observation.env))
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
    """Observe E2E children identified by their throwaway home or frontend cwd.

    The session frontend predates E2E environment layering, so its own
    .builds/build-<pid>_<timestamp> directory supplies the alternative run id.
    """
    return ProcessInspection().scan()


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


def _owner_live(
    owner_pid: int,
    *,
    command: Callable[[int], str | None],
    probe: Callable[[int, int], None],
    receipt_path: Path | None = None,
) -> bool:
    """Protect live pytest/xdist owners, including unreadable commands.

    PID liveness alone cannot distinguish a recycled PID. A readable command
    must still identify a test run; unknown identity never proves stale residue.
    """
    try:
        probe(owner_pid, 0)
    except ProcessLookupError:
        _write_cleanup_receipt(
            receipt_path, "owner", owner=owner_pid, probe="gone", preserved=False
        )
        return False
    except PermissionError:
        _write_cleanup_receipt(
            receipt_path, "owner", owner=owner_pid, probe="permission", preserved=True
        )
        return True
    cmdline = command(owner_pid)
    hints = [hint for hint in _LIVE_RUN_HINTS if cmdline is not None and hint in cmdline]
    preserved = cmdline is None or bool(hints)
    _write_cleanup_receipt(
        receipt_path,
        "owner",
        owner=owner_pid,
        probe="alive",
        command_known=cmdline is not None,
        hints=hints,
        preserved=preserved,
    )
    return preserved


def _identity_holds(pid: int, cmdline: str, *, command: Callable[[int], str | None]) -> bool:
    """Recheck normalized command identity immediately before signalling.

    A recycled PID must never receive the old process's signal. Unreadable
    commands cannot prove identity; ps transports may differ only in padding.
    """
    current = command(pid)
    if current is None:
        return False
    return " ".join(current.split()) == " ".join(cmdline.split())


@dataclass(frozen=True)
class ProcessInspection:
    """Native observation boundary used by discovery and pre-signal identity queries.

    Alternate transports supply observations, not ownership or signal decisions.
    The classifier and guarded cleanup remain in this component.
    """

    rows: Callable[[], list[tuple[int, str, str]]] = _ps_rows_with_env
    working_directory: Callable[[int], str | None] = _cwd_of
    group: Callable[[int], int] = os.getpgid
    command: Callable[[int], str | None] = _ps_command_of
    probe: Callable[[int, int], None] = os.kill

    def scan(self) -> list[E2EProcess]:
        """Query currently identifiable E2E processes, omitting vanished processes."""
        processes: list[E2EProcess] = []
        for pid, command, env in self.rows():
            observation = ProcessObservation(pid, command, env)
            cwd = None
            if _parse_run_id(env, _E2E_HOME_RUN_RE) is None and _looks_like_frontend(command):
                cwd = self.working_directory(pid)
            if E2EProcess.from_observation(observation, pgid=0, cwd=cwd) is None:
                continue
            try:
                pgid = self.group(pid)
            except (ProcessLookupError, PermissionError):
                continue
            process = E2EProcess.from_observation(observation, pgid=pgid, cwd=cwd)
            if process is not None:
                processes.append(process)
        return processes

    def owner_live(self, pid: int, *, receipt_path: Path | None = None) -> bool:
        """Preserve a live PID with a pytest/xdist or unreadable command."""
        return _owner_live(pid, command=self.command, probe=self.probe, receipt_path=receipt_path)

    def matches(self, process: E2EProcess) -> bool:
        """Recheck the observed command before signalling a potentially recycled PID."""
        return _identity_holds(process.pid, process.cmdline, command=self.command)


@dataclass(frozen=True)
class ResidueSweepPlan:
    """Immutable cleanup query result; never grants mutation of the process registry."""

    groups: frozenset[int]
    singles: frozenset[int]
    owners: frozenset[int]

    @classmethod
    def from_processes(
        cls,
        processes: list[E2EProcess],
        *,
        own_pid: int,
        own_pgrp: int,
        include_own: bool,
        owner_live: Callable[[int], bool],
    ) -> ResidueSweepPlan:
        """Plan cleanup while protecting concurrent runs and the caller's process group."""
        groups, singles, owners = _sweep_targets(
            processes,
            own_pid=own_pid,
            own_pgrp=own_pgrp,
            include_own=include_own,
            owner_live=owner_live,
        )
        return cls(frozenset(groups), frozenset(singles), frozenset(owners))


def _sweep_targets(
    procs: list[E2EProcess],
    *,
    own_pid: int,
    own_pgrp: int,
    include_own: bool,
    owner_live: Callable[[int], bool],
) -> tuple[set[int], set[int], set[int]]:
    """Select stale-run children, optionally including this caller's own run.

    Protect live foreign owners. Only a matched session leader outside our own
    group permits killpg; other members receive individual guarded signals.
    Every target's command identity is rechecked immediately before signalling.
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


def _signal_sweep_targets(
    plan: ResidueSweepPlan,
    process_by_pid: dict[int, E2EProcess],
    inspection: ProcessInspection,
    stop_signal: int,
    receipt_path: Path | None,
) -> None:
    """Recheck identity independently for each signal phase and target."""
    targets = [(pid, "group") for pid in plan.groups] + [(pid, "single") for pid in plan.singles]
    for pid, kind in targets:
        process = process_by_pid.get(pid) if kind == "group" else process_by_pid[pid]
        if process is None or not inspection.matches(process):
            _write_cleanup_receipt(
                receipt_path,
                "sweep_signal",
                target=pid,
                kind=kind,
                signal=stop_signal,
                outcome="identity_skip",
            )
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            _signal_cleanup(
                receipt_path,
                "sweep_signal",
                pid,
                stop_signal,
                os.killpg if kind == "group" else os.kill,
                owner=process.run[0],
                kind=kind,
            )


def sweep_stale_e2e_processes(
    *,
    include_own: bool = False,
    inspection: ProcessInspection | None = None,
    receipt_path: Path | None = None,
) -> int:
    """Reap guarded stale-run residue; optionally include our own package residue.

    Return the existing planned-target count, including identity-guard skips.
    Optional receipts observe decisions and signals without granting ownership.
    """
    inspection = inspection or ProcessInspection()
    procs = inspection.scan()
    plan = ResidueSweepPlan.from_processes(
        procs,
        own_pid=os.getpid(),
        own_pgrp=os.getpgrp(),
        include_own=include_own,
        owner_live=lambda pid: inspection.owner_live(pid, receipt_path=receipt_path),
    )
    groups, singles, owners = plan.groups, plan.singles, plan.owners
    _write_cleanup_receipt(receipt_path, "sweep", include_own=include_own, observed=len(procs))
    for proc in procs:
        _write_cleanup_receipt(
            receipt_path,
            "decision",
            target=proc.pid,
            group=proc.pgid,
            owner=proc.run[0],
            run_stamp=proc.run[1],
            selected=proc.pid in groups or proc.pid in singles,
        )
    if not groups and not singles:
        return 0
    process_by_pid = {proc.pid: proc for proc in procs}
    # Every signal lands only after the process's identity is re-verified — a
    # killed pytest's pids can be recycled, and an unverified killpg/os.kill
    # would hit whatever now holds them. A leader whose identity no longer
    # holds is skipped; its members are still in `singles` and verified there.
    _signal_sweep_targets(plan, process_by_pid, inspection, signal.SIGTERM, receipt_path)
    print(  # noqa: T201 -- must reach the terminal; loguru output is captured
        f"\nE2E RESIDUE: reaped {len(groups) + len(singles)} process(es) left by "
        f"dead pytest run(s) {sorted(owners)} (gateway/agent/daemon/frontend of a "
        "session that never ran its fixture teardowns)",
        file=sys.stderr,
    )
    time.sleep(_REAP_GRACE_SEC)
    _signal_sweep_targets(plan, process_by_pid, inspection, signal.SIGKILL, receipt_path)
    time.sleep(1.0)
    return len(groups) + len(singles)


def fixture_entrypoint() -> None:
    """Direct-process E2E serving injection, deliberately excluding root custody.

    This test-only module is explicitly selected by conftest; no environment
    flag or product entry point can bypass the real native birth gate.
    """
    import runpy

    from base.deploy.lifecycle import start_serving

    gate, module, *arguments = sys.argv[1:]
    path = Path(gate)

    def fixture_is_serving() -> bool:
        return path.is_file()

    @contextmanager
    def fixture_recovery() -> Generator[bool]:
        yield fixture_is_serving()

    start_serving.is_serving = fixture_is_serving
    start_serving.recovery_permitted = fixture_recovery
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


@pytest.fixture
def pty_sessions_proc() -> Iterator[None]:
    """The machine's pty-sessions service, which holds every agent shell a scenario opens.

    A real `ava start` runs it as a roster unit; here it is a direct process under the
    suite's AVA_HOME, stopped (sessions closed) with the test.
    """
    log_path = (
        Path(__file__).resolve().parents[2] / "tmp" / "e2e-logs" / f"pty-sessions-{os.getpid()}.log"
    )
    with managed_proc(
        [sys.executable, "-m", "services.agent_runner.pty_sessions.daemon"],
        env=os.environ.copy(),
        label="pty-sessions",
        log_path=str(log_path),
    ) as proc:
        deadline = time.monotonic() + 30.0
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"pty-sessions exited early; log tail:\n{proc_log_tail(str(log_path))}"
                )
            try:
                pty_client.request("ping")
                break
            except pty_client.ServiceUnavailableError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"pty-sessions never answered; log tail:\n{proc_log_tail(str(log_path))}"
                    ) from None
                time.sleep(0.1)
        yield


if __name__ == "__main__":
    fixture_entrypoint()
