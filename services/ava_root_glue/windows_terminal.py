"""Root-brokered birth of an independent Windows terminal Job owner.

The admission lock spans pending intent through the owner's ready receipt. The
owner is a root child outside every application Job. It survives application
stop, retains the original terminal Job handle, and records native zero
membership before closure. There is no requester-side process launch fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
from pydantic import BaseModel, ConfigDict

from services.ava_root.wiring import WiringContext
from shared.proc_tree import OwnedProcess
from shared.sessions.pty.allocation_freeze import locked_freeze_state
from shared.windows_terminal.backend import query
from shared.windows_terminal.record import NativeBirth, TerminalRecord, publish, read, record_path


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str
    command: str
    cwd: str
    env: dict[str, str]


class TerminalBroker:
    def __init__(self, context: WiringContext) -> None:
        self._lock = asyncio.Lock()
        self._accepting = False
        context.resource_handlers["terminal.start"] = self.request

    def start(self) -> None:
        self._accepting = True

    async def stop(self) -> None:
        self._accepting = False
        async with self._lock:
            pass  # every earlier birth has either a ready receipt or retained custody

    async def request(self, payload: dict[str, object]) -> object:
        request = StartRequest.model_validate(payload)
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("terminal admission is closed during root stop")
            # Shutdown waits for this admission rather than cancelling a native
            # creation thread whose target could otherwise appear after return.
            return (await asyncio.to_thread(start_terminal, request)).model_dump(mode="json")


def start_terminal(request: StartRequest) -> TerminalRecord:
    """Called only inside the root process by its explicit resource handler."""
    if sys.platform != "win32":
        raise RuntimeError("Windows terminal broker requires Windows")
    record_path(request.name)
    cwd = Path(request.cwd)
    if not cwd.is_absolute() or not cwd.is_dir() or not request.command.strip():
        raise ValueError("terminal requires an absolute existing cwd and a command")
    with locked_freeze_state() as freeze:
        prior = read(request.name)
        if prior is not None and prior.state != "closed":
            observed = query(prior)
            if observed.generation != freeze.generation:
                raise RuntimeError("terminal belongs to an earlier allocation generation")
            if observed.command != request.command or observed.cwd != request.cwd:
                raise RuntimeError("terminal name already belongs to a different command")
            return observed
        if freeze.status != "inactive":
            raise RuntimeError(f"terminal allocation is {freeze.status}; birth refused")
        record = TerminalRecord(
            name=request.name,
            domain=uuid.uuid4().hex,
            generation=freeze.generation,
            state="pending",
            root=NativeBirth.capture(OwnedProcess.capture(psutil.Process())),
            started_at=time.time(),
            command=request.command,
            cwd=request.cwd,
        )
        publish(record, previous=prior)
        return _launch_owner(record, request.env)


def _launch_owner(record: TerminalRecord, environment: dict[str, str]) -> TerminalRecord:
    from shared.paths import logs_dir

    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{record.name}.owner.log").open("ab", buffering=0) as output:
        # Root itself is outside application Jobs. No BREAKAWAY flag is needed
        # or permitted; ordinary service/agent processes cannot use this path.
        owner = subprocess.Popen(
            [sys.executable, "-m", "services.ava_root_glue.windows_terminal_owner"],
            cwd=Path(__file__).resolve().parents[2],
            env=dict(os.environ),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        launcher = NativeBirth.capture(OwnedProcess.capture(psutil.Process(owner.pid)))
        pending = TerminalRecord.model_validate(record.model_dump() | {"launcher": launcher})
        publish(pending, previous=record)
        if owner.stdin is None:
            raise RuntimeError("terminal owner launch omitted its birth channel")
        with owner.stdin:
            owner.stdin.write(json.dumps({"name": record.name, "env": environment}).encode())
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            current = read(record.name)
            if current is None or current.domain != record.domain:
                raise RuntimeError("terminal birth custody changed")
            if current.state != "pending":
                if current.root != record.root or current.launcher != launcher:
                    raise RuntimeError("terminal receipt changed its birth authority")
                return current if current.state == "closed" else query(current)
            if owner.poll() is not None:
                raise RuntimeError("terminal owner exited before ready; custody retained")
            time.sleep(0.02)
    raise TimeoutError("terminal birth remains unacknowledged; custody retained")
