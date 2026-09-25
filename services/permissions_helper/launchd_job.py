"""The launchd job surface of the macOS permissions helper.

One home for the per-cluster LaunchAgent identity — label, plist path, launchd
domain — and for bounded ``launchctl print`` inspection of the job. The
build/cert/repair steps stay in ``lifecycle``; that module delegates the job
identity here so the label formula exists once, and the helper healthcheck
(task #3393) reads and parses the job here so the ``launchctl print`` field
vocabulary exists once too.

The parse reads the ``job state`` line deliberately: a stuck job's top-level
``state`` still reads ``spawn scheduled``/``xpcproxy`` — ``spawn failed`` lives
only in ``job state`` (F5 findings section 6; PR #2500 review). Reads are
bounded and total: a hung tool or an absent job folds into a sentinel result,
never an exception — the callers are healthchecks whose whole point is to
answer even when the system under them does not.
"""

from __future__ import annotations

import errno
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil

from shared.proc import run_bounded
from shared.proc_tree import OwnedProcess, capture_tree, leader_owns_pids

HELPER_BUNDLE_ID = "com.ava.permissions-helper"
"""Fixed across clusters so one TCC grant covers all (the grant keys on this)."""

_READ_TIMEOUT_S = 30.0
"""Bound for one launchctl query — local IPC with launchd."""

_TIMED_OUT_RC = 124
"""Stand-in exit status for a read the bound killed (`timeout(1)`'s convention;
the real launchd codes are far below this)."""


def helper_job_label(home: Path | None = None) -> str:
    """This cluster's helper LaunchAgent label (``<bundle id>.<home slug>``).

    Per-cluster, keyed on the home-path slug (path-only identity); the bundle id
    (the TCC grant) stays shared across clusters.
    """
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return f"{HELPER_BUNDLE_ID}.{home_slug(ava_home() if home is None else home)}"


def helper_job_domain() -> str:
    """The launchd domain this login session's jobs live in (``gui/<uid>``)."""
    return f"gui/{os.getuid()}"


def helper_job_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def helper_job_plist_path(home: Path | None = None) -> Path:
    return helper_job_agents_dir() / f"{helper_job_label(home)}.plist"


def helper_stop_intent(home: Path) -> bool:
    """Read native shutdown intent; corrupt or foreign inputs are never absence."""
    path = home / "run" / "ava-root" / "helper-stopped"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or path.read_bytes() != b"stopped\n"
    ):
        raise RuntimeError("helper stop intent is unreadable or invalid; custody retained")
    return True


def clear_helper_stop_intent(home: Path) -> None:
    """Explicit start only, after positive native job absence."""
    if not helper_stop_intent(home):
        return
    path = home / "run" / "ava-root" / "helper-stopped"
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _retirement_query(target: str, deadline: float) -> str | None:
    """Absence is one positive launchd result; all other failures are unknown."""
    result = _retirement_command(["print", target], deadline)
    if result.returncode == 113 and b"Could not find service" in result.stderr:
        return None
    if result.returncode:
        raise RuntimeError(f"cannot inspect helper job {target}: {result.stderr!r}")
    return result.stdout.decode()


def _retirement_command(args: list[str], deadline: float) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    return run_bounded(["launchctl", *args], timeout=remaining, capture_output=True)


def _retirement_plist(path: Path, home: Path, socket_path: Path) -> tuple[bytes, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError(f"helper plist is not an owned immutable input: {path}")
    original = path.read_bytes()
    data = cast("dict[str, object]", plistlib.loads(original))
    environment = data["EnvironmentVariables"]
    argv = data["ProgramArguments"]
    if not isinstance(environment, dict) or not isinstance(argv, list):
        raise TypeError(f"helper plist has invalid launch inputs: {path}")
    environment = cast("dict[str, object]", environment)
    argv = cast("list[object]", argv)
    if (
        data["Label"] != helper_job_label(home)
        or data.get("KeepAlive") != {"SuccessfulExit": False}
        or environment.get("AVA_PERMISSIONS_HELPER_SOCKET") != str(socket_path)
        or environment.get("AVA_PERMISSIONS_HELPER_ROOT_SEED")
        != str(home / "run" / "ava-root" / "seed.json")
        or len(argv) != 1
        or not isinstance(argv[0], str)
    ):
        raise RuntimeError(f"helper plist does not bind the exact home and shutdown policy: {path}")
    return original, argv[0]


def _retirement_owner(state: str, socket_path: Path, executable: str) -> OwnedProcess:
    from services.permissions_helper import client

    match = re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
    if match is None:
        raise RuntimeError("loaded helper lacks a native owner; retirement requires reconciliation")
    owner = OwnedProcess.capture(psutil.Process(int(match[1])))
    if (
        not owner.live()
        or Path(psutil.Process(owner.pid).exe()).resolve() != Path(executable).resolve()
    ):
        raise RuntimeError("loaded helper executable does not match its home job")
    if leader_owns_pids(owner, {os.getpid()}):
        raise RuntimeError("cannot unregister the caller's permissions-helper ancestor")
    reply = client.ping(sock_path=socket_path)
    if (
        reply.get("pid") != owner.pid
        or reply.get("root_stop_intent_v1") is not True
        or reply.get("helper_shutdown_v1") is not True
    ):
        raise RuntimeError("helper socket does not prove this native job and stop protocol")
    _require_empty_helper(owner, socket_path)
    return owner


def _require_empty_helper(owner: OwnedProcess, socket_path: Path) -> None:
    from services.permissions_helper import client

    root = client.root_status(sock_path=socket_path)
    stopped = root["state"] == "stopped" and root["stop_requested"] is True
    unseeded = root["state"] == "unseeded" and root["seeded"] is False
    if root.get("pid") is not None or not (stopped or unseeded):
        raise RuntimeError("helper still owns an active or uncertain root")
    if client.session_list(sock_path=socket_path) or capture_tree(owner) != {owner}:
        raise RuntimeError("helper still owns native children; stop them before unregistering")


def unregister_helper(
    home: Path, *, helper_port: int, timeout_s: float = 30.0, force: bool = False
) -> None:
    """Retire only this home's stopped helper; uncertainty retains its registry slot.

    The caller must first close root services and broker sessions. No foreign PID is signalled,
    no shared signed bundle is deleted, and a descendant cannot unload its ancestor.
    """
    if sys.platform != "darwin":
        return
    if not home.is_absolute() or home.resolve() != home or not 0 < helper_port < 65536:
        raise ValueError("helper retirement requires a canonical home and registered port")
    from services.ava_root.custody import require_clear
    from services.ava_root.singleton import acquire_instance_lock, release_instance_lock

    run_dir = home / "run" / "ava-root"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_clear(run_dir)
    deadline = time.monotonic() + timeout_s
    original = None if force else _request_helper_shutdown(home, helper_port, deadline)
    lock_fd = acquire_instance_lock(run_dir)
    try:
        require_clear(run_dir)
        _unregister_stopped_helper(home, helper_port, deadline, force=force, expected=original)
    finally:
        release_instance_lock(lock_fd)


def _request_helper_shutdown(home: Path, port: int, deadline: float) -> bytes | None:
    from services.permissions_helper import client

    target = f"{helper_job_domain()}/{helper_job_label(home)}"
    state = _retirement_query(target, deadline)
    if state is None:
        return None
    path = helper_job_plist_path(home)
    socket_path = home / "run" / f"permissions-helper.{port}.sock"
    original, executable = _retirement_plist(path, home, socket_path)
    if _job_pid(state) is None:
        _require_idle_job(state, home)
        return original
    owner = _retirement_owner(state, socket_path, executable)
    if path.is_symlink() or path.read_bytes() != original:
        raise RuntimeError("helper definition changed before shutdown")
    reply = client.shutdown_helper(home / "run" / "ava-root", sock_path=socket_path)
    if reply != {"stopping": True, "pid": owner.pid, "run_dir": str(home / "run" / "ava-root")}:
        raise RuntimeError("helper shutdown did not acknowledge exact-home native custody")
    _wait_retirement_owner(owner, deadline)
    while (latest := _retirement_query(target, deadline)) is not None:
        if _job_pid(latest) is None:
            _require_idle_job(latest, home)
            break
        if _job_pid(latest) != owner.pid:
            raise RuntimeError("helper relaunched after acknowledged shutdown; custody retained")
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return original


def _require_idle_job(state: str, home: Path) -> None:
    if (
        _job_pid(state) is not None
        or re.search(r"^\s*state = not running\s*$", state, re.MULTILINE) is None
        or re.search(r"^\s*last exit code = 0\s*$", state, re.MULTILINE) is None
        or not helper_stop_intent(home)
    ):
        raise RuntimeError("helper job is not positively idle with retained shutdown intent")


def _unregister_stopped_helper(
    home: Path, port: int, deadline: float, *, force: bool, expected: bytes | None
) -> None:
    target = f"{helper_job_domain()}/{helper_job_label(home)}"
    path = helper_job_plist_path(home)
    state = _retirement_query(target, deadline)
    socket_path = home / "run" / f"permissions-helper.{port}.sock"
    if state is None and not path.exists() and not path.is_symlink():
        _require_absent_socket(socket_path, deadline)
        return
    original, executable = _retirement_plist(path, home, socket_path)
    if expected is not None and original != expected:
        raise RuntimeError("helper definition changed during shutdown; preserving it")
    if state is None:
        # An earlier completed native stop may have retained the definition.
        # A socket still accepting connections contradicts that absence, even
        # when launchd no longer owns the listener. Never restart it to retire it.
        _require_absent_socket(socket_path, deadline)
        if _retirement_query(target, deadline) is not None:
            raise RuntimeError("helper job appeared during retirement; preserving its definition")
        _unlink_unchanged_plist(path, original)
        return
    if _job_pid(state) is not None:
        if not force:
            raise RuntimeError("helper retirement still has a native owner; custody retained")
        owner = _retirement_owner(state, socket_path, executable)
        latest = _retirement_query(target, deadline)
        latest_pid = _job_pid(latest)
        if path.is_symlink() or path.read_bytes() != original or latest_pid != owner.pid:
            raise RuntimeError("helper job changed before retirement")
        # Explicit force revokes native restart authority before any escalation.
        _bootout_stopped_helper(target, deadline)
        _force_retirement_owner(owner, deadline)
    else:
        _require_idle_job(state, home)
        _require_absent_socket(socket_path, deadline)
        _bootout_stopped_helper(target, deadline)
    _unlink_unchanged_plist(path, original)


def _job_pid(state: str | None) -> int | None:
    match = None if state is None else re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
    return None if match is None or int(match[1]) <= 0 else int(match[1])


def _force_retirement_owner(owner: OwnedProcess, deadline: float) -> None:
    # A fresh native birth check is required at each signal, without the legacy
    # tolerance used for older persisted process records elsewhere.
    if deadline <= time.monotonic():
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    try:
        process = psutil.Process(owner.pid)
        if OwnedProcess.capture(process) != owner:
            raise RuntimeError("helper native identity changed before stop")
        process.kill()
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise TimeoutError("helper did not exit; definition and custody retained") from exc


def _wait_retirement_owner(owner: OwnedProcess, deadline: float) -> None:
    try:
        process = psutil.Process(owner.pid)
        if OwnedProcess.capture(process) != owner:
            return
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise TimeoutError("helper shutdown did not complete; custody retained") from exc


def _bootout_stopped_helper(target: str, deadline: float) -> None:
    result = _retirement_command(["bootout", target], deadline)
    if result.returncode:
        raise RuntimeError(f"helper bootout failed; custody retained: {result.stderr!r}")
    while _retirement_query(target, deadline) is not None:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _require_absent_socket(path: Path, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("cannot prove helper socket absence from a non-socket input")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining)
        try:
            connection.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return
            raise RuntimeError("cannot prove helper socket absence; custody retained") from exc
    raise RuntimeError("helper socket remains live without native job custody")


def _unlink_unchanged_plist(path: Path, original: bytes) -> None:
    if path.is_symlink() or path.read_bytes() != original:
        raise RuntimeError("helper plist changed during retirement; preserving it")
    path.unlink()


def read_helper_job() -> str | None:
    """One ``launchctl print`` dump of this cluster's helper job.

    None when launchd has no such job — and equally when the read itself could
    not run (a call the bound killed): every caller's next question is what the
    job state says, and a read that says nothing reads as an absent job.
    """
    cmd = ["launchctl", "print", f"{helper_job_domain()}/{helper_job_label()}"]
    try:
        proc = run_bounded(cmd, timeout=_READ_TIMEOUT_S, capture_output=True)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


@dataclass(frozen=True, slots=True)
class HelperJobState:
    """The launchd facts the helper healthcheck classifies from."""

    job_state: str | None
    """``job state`` value — the line that carries ``spawn failed``."""

    last_exit_code: str | None
    """``last exit code`` value (e.g. ``78: EX_CONFIG``); None before any exit."""

    needs_lwcr_update: bool
    """Whether the dump carries the ``needs LWCR update`` properties marker."""

    btm_uuid: str | None
    """The job's recorded BTM uuid, when launchd shows one."""

    runs: int | None
    """Launchd's spawn count, when the dump shows one."""


_JOB_STATE_RE = re.compile(r"^\s*job state = (.+)$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\s*last exit code = (.+)$", re.MULTILINE)
_BTM_UUID_RE = re.compile(r"^\s*BTM uuid = (.+)$", re.MULTILINE)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)$", re.MULTILINE)

_NEEDS_LWCR_MARKER = "needs LWCR update"


def parse_job_state(text: str) -> HelperJobState:
    """Parse one ``launchctl print`` dump; unknown lines are ignored (pure)."""
    return HelperJobState(
        job_state=_capture(_JOB_STATE_RE, text),
        last_exit_code=_capture(_LAST_EXIT_RE, text),
        needs_lwcr_update=_NEEDS_LWCR_MARKER in text,
        btm_uuid=_capture(_BTM_UUID_RE, text),
        runs=_runs(_RUNS_RE, text),
    )


def _capture(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return None if match is None else match.group(1).strip()


def _runs(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return None if match is None else int(match.group(1))
