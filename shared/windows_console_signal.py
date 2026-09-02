"""One-shot private-console signal sender; never attach the calling daemon.

Invoked by absolute loaded-image file path under isolated Python. This leaf
does not import Ava settings, inspect cwd, or load code from PYTHONPATH.
Successful return means Windows accepted a request, not that a target exited.
"""

import ctypes
import json
import os
import sys
import threading
from pathlib import Path

import psutil

PRIVATE_CONSOLE = "private-console-v1"


def send_private_console_break(record_path: Path, pid: int, birth: float) -> None:
    """Reject stale identity, legacy records and any foreign console member."""
    record = json.loads(record_path.read_text())
    if (record["pid"], record["create_time"], record.get("control_mode")) != (
        pid,
        birth,
        PRIVATE_CONSOLE,
    ):
        raise RuntimeError("private console record identity changed or is unproven")
    target = psutil.Process(pid)
    if target.create_time() != birth or not target.is_running():
        raise RuntimeError("private console target identity changed")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        raise ctypes.WinError(ctypes.get_last_error())
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)
    handled = threading.Event()

    def ignore_own_event(_event: int) -> int:
        handled.set()
        return 1

    handler = handler_type(ignore_own_event)
    if not kernel.SetConsoleCtrlHandler(handler, 1):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        members_buffer = (ctypes.c_ulong * 4096)()
        count = kernel.GetConsoleProcessList(members_buffer, len(members_buffer))
        if count <= 0 or count > len(members_buffer):
            raise RuntimeError("private console membership unavailable or exceeds bound")
        members = set(members_buffer[:count])
        allowed = {pid, os.getpid(), *(child.pid for child in target.children(recursive=True))}
        if pid not in members or not members <= allowed:
            raise RuntimeError("private console contains an unrelated process")
        for path in record_path.parent.glob("*.json"):
            other = json.loads(path.read_text())
            other_pid = other["pid"]
            if other_pid in members and other_pid != pid:
                raise RuntimeError("private console contains another recorded session")
        # Recheck after attachment/enumeration. No numeric-PID check eliminates
        # the final OS check-to-signal race; it is not an exactly-once primitive.
        if target.create_time() != birth or not target.is_running():
            raise RuntimeError("private console target exited before delivery")
        if json.loads(record_path.read_text()) != record:
            raise RuntimeError("private console record changed before delivery")
        # NEW_CONSOLE ignores NEW_PROCESS_GROUP. Broadcast is confined to this
        # explicitly private, enumerated console; never infer a group from PID.
        if not kernel.GenerateConsoleCtrlEvent(1, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        # FreeConsole resets the process handler table. Do not detach before
        # our own queued broadcast has reached the protective handler.
        if not handled.wait(1):
            raise RuntimeError("private console helper did not observe its own control event")
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    try:
        send_private_console_break(Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]))
    except Exception as error:
        sys.stderr.write(f"private console delivery refused: {error}\n")
        raise SystemExit(1) from error
