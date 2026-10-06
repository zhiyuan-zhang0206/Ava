"""Shared primitives for the two-section chain smoke: failures, probes, evidence IO."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

_ROOT_SOCKET = "ava-root.sock"
_ROOT_WAIT_S = 30.0
_POLL_S = 0.25
_UNIT_IDS = ("heartbeat", "heartbeat-b", "heartbeat-c")


_UNIT_PROBE = '''\
\
"""Unit probe: side-effect-free preflight queries + heartbeat; answers sampled rounds.

TCCAccessPreflight never prompts and never touches protected data; tccd still
records every call with this process's attribution, which the smoke reads back
to prove the launchd -> helper -> root -> unit chain resolves to the helper.
"""

import ctypes
import json
import os
import sys
import time

SERVICES = [
    "kTCCServiceSystemPolicyDesktopFolder",
    "kTCCServiceSystemPolicyAllFiles",
    "kTCCServiceScreenCapture",
]

_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
_tcc.TCCAccessPreflight.restype = ctypes.c_int
_tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def preflight(service):
    cf_service = _cf.CFStringCreateWithCString(None, service.encode(), 0x08000100)
    return _tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None)


def main():
    results_path, beat_path = sys.argv[1], sys.argv[2]
    request_path = results_path + ".req"
    rounds_path = results_path + ".rounds"
    results = {}
    for service in SERVICES:
        results[service] = preflight(service)
    with open(results_path, "w") as handle:
        json.dump({"pid": os.getpid(), "ppid": os.getppid(), "services": results}, handle)
    seen = None
    while True:
        with open(beat_path, "a") as handle:
            handle.write("%.0f\\n" % time.time())
        try:
            with open(request_path) as handle:
                current = handle.read().strip()
        except OSError:
            current = None
        if current and current != seen:
            seen = current
            record = {
                "round": current,
                "ts": round(time.time(), 3),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "pgid": os.getpgid(0),
                "sid": os.getsid(0),
                "services": {service: preflight(service) for service in SERVICES},
            }
            with open(rounds_path, "a") as handle:
                handle.write(json.dumps(record) + "\\n")
        time.sleep(1)


main()
'''


class SmokeError(RuntimeError):
    """One smoke phase failed; the message carries the phase and the detail."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"FAIL(phase={phase}): {detail}")
        self.phase = phase
        self.detail = detail


def _fail(phase: str, detail: str) -> NoReturn:
    """Raise one phase failure (kept out of the try bodies for TRY301)."""
    raise SmokeError(phase, detail)


def _run(
    cmd: list[str], *, check: bool = True, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv lists built in this file, no untrusted input
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip()[-800:]
        _fail("run", f"{cmd[0]} exited {proc.returncode}: {tail}")
    return proc


def _wait_for(what: str, predicate, timeout: float, phase: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(_POLL_S)
    _fail(phase, f"timed out after {timeout:.0f}s waiting for {what}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps_line(*pids: int) -> str:
    proc = _run(["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", ",".join(str(p) for p in pids)])
    return proc.stdout.strip()


def _save(evidence: Path, name: str, text: str) -> None:
    (evidence / name).write_text(text + "\n")


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _bootout(label: str) -> None:
    _run(["launchctl", "bootout", f"{_launchctl_domain()}/{label}"], check=False, timeout=30)


def _job_pid(label: str) -> int | None:
    proc = _run(["launchctl", "list"], check=False, timeout=30)
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            pid_field = parts[0].strip()
            return int(pid_field) if pid_field.isdigit() else None
    return None


def _kill(pid: int, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text()[-limit:]
    except OSError:
        return "<missing>"


def _refusal(call: Callable[[], object]) -> str:
    """The error text of a call that must be refused ("" when it was accepted)."""
    try:
        call()
    except Exception as exc:  # the refusal message is the assertion
        return str(exc)
    return ""


# A status/ping poll that cannot get an answer yet: socket absent or refusing (OSError),
# empty or malformed reply (ValueError), helper error reply (PermissionsHelperError and
# SmokeError are RuntimeErrors). Anything else is a bug in the poll and surfaces.
_NOT_ANSWERING = (OSError, ValueError, RuntimeError)


def _ping_or_none(helper_client, sock: Path):
    try:
        return helper_client.ping(sock_path=sock)["pong"]
    except _NOT_ANSWERING:
        return None


def _status_if_running(root_status, *, excluding: int | None = None):
    try:
        status = root_status()
    except _NOT_ANSWERING:
        return None
    if status["root"]["running"] and isinstance(status["root"]["pid"], int):
        pid = int(status["root"]["pid"])
        if excluding is not None and pid == excluding:
            return None
        return status
    return None


def _status_if_keeper_running(helper_root_status):
    try:
        status = helper_root_status()
    except _NOT_ANSWERING:
        return None
    if status.get("state") == "running" and isinstance(status.get("pid"), int):
        return status
    return None


def _status_if_conflict(helper_root_status):
    try:
        status = helper_root_status()
    except _NOT_ANSWERING:
        return None
    return status if status.get("state") == "conflict" else None


def _probe_pids(probe: Path) -> set[int]:
    """Every live unit-probe process of this workdir, whoever spawned it."""
    return {int(pid) for pid in _run(["pgrep", "-f", str(probe)], check=False).stdout.split()}


def _relaunched_helper(label: str, old_pid: int):
    pid = _job_pid(label)
    if pid is None or pid == old_pid:
        return None
    return pid


def _unit_entry(status: dict, unit_id: str) -> dict:
    for entry in status["units"]:
        if entry["id"] == unit_id and isinstance(entry["pid"], int):
            return entry
    _fail("chain", f"unit {unit_id!r} has no live pid in {json.dumps(status)[:400]}")


def _ppid_of(pid: int) -> int:
    proc = _run(["ps", "-o", "ppid=", "-p", str(pid)])
    return int(proc.stdout.strip())


def _root_call(run_dir: Path, verb: str) -> dict[str, Any]:
    """Send one K1 verb over the raw control socket (stdlib only).

    The smoke drives the root as a black box — process tree, socket, K1
    verbs — so it never depends on the root package's client API.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(str(run_dir / _ROOT_SOCKET))
        sock.sendall(json.dumps({"verb": verb}).encode() + b"\n")
        line = sock.makefile("rb").readline()
    response = json.loads(line)
    if not response.get("ok"):
        _fail("root", f"{verb} refused: {response}")
    return cast("dict[str, Any]", response["result"])
