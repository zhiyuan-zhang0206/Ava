"""One terminal resource's independent native Job owner; never a service launcher."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field

from services.ava_root.custody import ServiceCustody
from services.ava_root.windows.process import ApplicationProcess, spawn
from shared.native_process.ownership import OwnedProcess
from shared.root_control.ipc import (
    MAX_MESSAGE_BYTES,
    ErrorCode,
    encode,
    error_response,
    ok_response,
)
from shared.root_control.windows.native import command_argv
from shared.root_control.windows.transport import PipeServer
from shared.windows_terminal.record import NativeBirth, TerminalRecord, endpoint, publish, read


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    domain: str
    verb: Literal["status", "close"]
    force: bool
    timeout: float = Field(gt=0, le=25, allow_inf_nan=False)


class BirthEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str
    env: dict[str, str]


def _owned_birth(record: TerminalRecord) -> NativeBirth:
    """Accept only the brokered root child (including its exact venv redirector)."""
    launcher = record.launcher
    if launcher is None or not record.root.identity().live() or not launcher.identity().live():
        raise RuntimeError("terminal birth authority is no longer live")
    current = psutil.Process()
    owner = NativeBirth.capture(OwnedProcess.capture(current))
    if psutil.Process(launcher.pid).ppid() != record.root.pid:
        raise RuntimeError("terminal launcher is outside its recorded root")
    if owner != launcher:
        if current.ppid() != launcher.pid:
            raise RuntimeError("terminal owner is not its recorded launcher's direct child")
        if current.cmdline()[1:] != psutil.Process(launcher.pid).cmdline()[1:]:
            raise RuntimeError("terminal redirector changed the owner command")
    if not record.root.identity().live() or not launcher.identity().live():
        raise RuntimeError("terminal birth authority changed during validation")
    return owner


def _command(record: TerminalRecord, env: dict[str, str]) -> tuple[list[str], str | None]:
    if set(record.command) & set("&|<>^%"):
        command = str(Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe")
        return [command], f'"{command}" /s /c "{record.command}"'
    planned = command_argv(record.command)
    if planned[0] == ".venv/bin/python":
        planned[0] = str(Path(record.cwd) / ".venv" / "Scripts" / "python.exe")
    executable = shutil.which(planned[0], path=env.get("PATH", ""))
    if executable is None:
        candidate = Path(record.cwd) / planned[0]
        executable = str(candidate) if candidate.is_file() else None
    if executable is None or not Path(executable).is_absolute():
        raise ValueError("terminal executable cannot be resolved to an absolute path")
    return [executable, *planned[1:]], None


class TerminalOwner:
    def __init__(
        self, record: TerminalRecord, process: ApplicationProcess, custody: ServiceCustody
    ) -> None:
        self.record, self.process, self.custody = record, process, custody
        self.done = asyncio.Event()
        self._lock = asyncio.Lock()
        self._waiter = asyncio.create_task(process.wait())

    async def finish(self) -> None:
        if self.process.job.active_processes():
            raise RuntimeError("terminal Job is not empty")
        await self._waiter
        self.custody.clear()
        closed = TerminalRecord.model_validate(
            self.record.model_dump()
            | {
                "state": "closed",
                "empty_job_observed": True,
                "closed_at": time.time(),
            }
        )
        publish(closed, previous=self.record)
        self.record = closed
        self.process.job.close()
        self.done.set()

    async def observe(self) -> None:
        while not self.done.is_set():
            async with self._lock:
                if self.done.is_set():
                    return
                if not self.process.job.active_processes():
                    await self.finish()
                    return
            await asyncio.sleep(0.05)

    async def handle(self, raw: bytes) -> bytes:
        try:
            request = ControlRequest.model_validate_json(raw)
            if request.domain != self.record.domain:
                return encode(error_response(ErrorCode.INVALID_REQUEST, "terminal domain changed"))
            async with self._lock:
                if request.verb == "close" and self.record.state != "closed":
                    await self.process.close(
                        self.custody, timeout=request.timeout, force=request.force
                    )
                    await self.finish()
                return encode(ok_response(self.record.model_dump(mode="json")))
        except Exception as exc:
            return encode(error_response(ErrorCode.INTERNAL, f"terminal control refused: {exc}"))


async def run(name: str, env: dict[str, str]) -> None:
    from shared.paths import logs_dir, run_dir

    pending = read(name)
    if pending is None or pending.state != "pending":
        raise RuntimeError("terminal owner requires its pending birth intent")
    owner = _owned_birth(pending)
    custody = ServiceCustody(run_dir() / "windows-terminal-domains" / pending.domain, "terminal")
    argv, command_line = _command(pending, env)
    with (logs_dir() / f"{name}.out.log").open("ab", buffering=0) as output:
        process = spawn(
            argv, env, output.fileno(), cwd=Path(pending.cwd), command_line=command_line
        )
    try:
        members = process.members()
        custody.retain(members)
        target = next(iter(sorted(members, key=lambda item: item.pid)), None)
        # A short command may exit before capture. The retained original Job can
        # still prove complete closure, but cannot invent a running target birth.
        record = TerminalRecord.model_validate(
            pending.model_dump()
            | {
                "owner": owner,
                "target": None if target is None else NativeBirth.capture(target),
                "state": "pending" if target is None else "running",
            }
        )
        terminal = TerminalOwner(record, process, custody)
        server = PipeServer(endpoint(record), terminal.handle, lambda _raw: None)
        await server.start()
        publish(record, previous=pending)
        monitor = asyncio.create_task(terminal.observe())
        try:
            done = asyncio.create_task(terminal.done.wait())
            completed, _ = await asyncio.wait({monitor, done}, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await server.close()
    finally:
        # Failure closes the native domain but deliberately leaves unacknowledged
        # custody; neither owner death nor KILL_ON_CLOSE fabricates a receipt.
        process.job.close()


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("native terminal owner requires Windows")
    raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("terminal birth envelope exceeds protocol limit")
    envelope = BirthEnvelope.model_validate_json(raw)
    asyncio.run(run(envelope.name, envelope.env))


if __name__ == "__main__":
    main()
