"""Bounded one-shot Ctrl-Break delivery to a captured application Job console.

The caller supplies an unchanged custody snapshot of native Job membership.
Every console member must match one captured birth; this module never consults
named-session registries or changes the root supervisor's own console.
"""

import ctypes
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import psutil


def _custody(path: Path, digest: str, pid: int) -> tuple[bytes, dict[int, float]]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise RuntimeError("application custody changed before delivery")
    record = json.loads(content)
    births = {row["pid"]: row["birth"] for row in record["processes"]}
    if pid not in births or psutil.Process(pid).create_time() != births[pid]:
        raise RuntimeError("application console birth changed")
    return content, births


def _console_members(kernel: Any, births: dict[int, float], pid: int) -> set[int]:
    members_buffer = (ctypes.c_uint32 * 4096)()
    count = kernel.GetConsoleProcessList(members_buffer, len(members_buffer))
    if count <= 0 or count > len(members_buffer):
        raise RuntimeError("application console membership unavailable")
    members = set(members_buffer[:count]) - {os.getpid()}
    if pid not in members or not members <= births.keys():
        raise RuntimeError("application console includes an unowned process")
    for member in members:
        if psutil.Process(member).create_time() != births[member]:
            raise RuntimeError("application console native birth changed")
    return members


def _attach_console(kernel: Any, pid: int) -> bool:
    kernel.FreeConsole()
    if kernel.AttachConsole(pid):
        return True
    code = cast(Any, ctypes).get_last_error()
    if code == 6:  # ERROR_INVALID_HANDLE: this captured member has no console.
        return False
    raise OSError(code, f"AttachConsole({pid}) failed with Win32 error {code}")


def deliver(path: Path, digest: str, pid: int, deadline: float) -> dict[str, object]:
    if sys.platform != "win32":
        raise RuntimeError("application console delivery requires Windows")
    content, births = _custody(path, digest, pid)
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    if not _attach_console(kernel, pid):
        # Job members include console hosts and processes that detached. Their
        # absence from a console is not delivery or closure: the owner must
        # still signal the other captured consoles and observe an empty Job.
        return {"delivered_pids": [], "consoleless_pid": pid}
    handled = threading.Event()
    callback_type = cast(Any, ctypes).WINFUNCTYPE(ctypes.c_int32, ctypes.c_uint32)

    def own_event(_event: int) -> int:
        handled.set()
        return 1

    callback = callback_type(own_event)
    try:
        if not kernel.SetConsoleCtrlHandler(callback, 1):
            raise OSError("cannot protect the isolated console signal sender")
        members = _console_members(kernel, births, pid)
        if path.read_bytes() != content or time.monotonic() >= deadline:
            raise RuntimeError("application console evidence changed or expired")
        if not kernel.GenerateConsoleCtrlEvent(1, 0):
            raise OSError("application Ctrl-Break was rejected")
        if not handled.wait(min(1.0, max(0.0, deadline - time.monotonic()))):
            raise TimeoutError("console sender did not observe its own event")
        return {"delivered_pids": sorted(members)}
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    result = deliver(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
    sys.stdout.write(json.dumps(result) + "\n")
