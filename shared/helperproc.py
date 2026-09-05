"""macOS process sessions spawned directly by the permissions helper.

The helper's in-memory child table is intentionally not lifecycle state: it is
lost whenever the helper restarts. The ordinary ``SessionRecord`` files under
``$AVA_HOME/run/sessions`` remain the durable session registry, so liveness,
enumeration, timestamps, and kills keep working across helper replacement.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from importlib import import_module
from pathlib import Path
from typing import Protocol, TypedDict, cast

import psutil

from shared.log import logger
from shared.paths import logs_dir, run_dir
from shared.session_backend import SessionBackend
from shared.session_record import SessionRecord, pid_starttime_ticks

_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)
_CREATE_TIME_TOLERANCE_S = 2.0
_DEAD_CHILD_SENTINEL = -1.0
_KILL_POLL_S = 0.05
_KILL_CONFIRM_TIMEOUT_S = 5.0


class _SpawnResult(TypedDict):
    pid: int
    reused: bool


class _HelperClient(Protocol):
    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> _SpawnResult: ...


def _sessions_dir() -> Path:
    directory = run_dir() / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record_path(name: str) -> Path:
    return _sessions_dir() / f"{name}.json"


def _read_record(name: str) -> SessionRecord | None:
    return SessionRecord.read(_record_path(name))


def session_log_path(name: str) -> Path:
    """The combined stdout/stderr log for a helper-spawned session."""
    return logs_dir() / f"{name}.out.log"


def _process_is_live(process: psutil.Process) -> bool:
    """Whether a process can still run; an unreaped zombie is already dead."""
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _process_for_record(record: SessionRecord) -> psutil.Process | None:
    """Resolve the live process identified by a persistent session record."""
    try:
        process = psutil.Process(record.pid)
        if not _process_is_live(process):
            return None
        if record.starttime is not None:
            return process if record.identifies(record.pid) is True else None
        if abs(process.create_time() - record.create_time) > _CREATE_TIME_TOLERANCE_S:
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return process


def _record_reapable(record: SessionRecord) -> tuple[bool, str]:
    """Whether a failed identity check proves the record can be discarded."""
    if _process_for_record(record) is not None:
        return False, "process is still live"
    try:
        process = psutil.Process(record.pid)
        if not process.is_running() or not _process_is_live(process):
            return True, "pid is no longer running"
    except psutil.NoSuchProcess:
        return True, "pid is gone"
    except (psutil.AccessDenied, OSError):
        return False, "pid could not be inspected"
    if record.identifies(record.pid) is False:
        return True, "pid was reused by another process"
    return False, "live pid did not satisfy the legacy identity check"


def spawn_via_helper(
    name: str,
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str],
    stdout: Path,
    stderr: Path,
) -> int:
    """Ask the permissions helper to spawn one named direct child.

    Failure is deliberately loud. Falling back to ``shared._reparent`` would
    launch successfully with the wrong TCC responsibility identity.
    """
    # Resolve the upper-layer wire adapter lazily. The lifecycle policy stays
    # in shared while the signed helper's transport client remains a service.
    client = cast("_HelperClient", import_module("services.permissions_helper.client"))

    try:
        result = client.spawn_process(
            name,
            argv,
            env,
            str(cwd),
            str(stdout),
            str(stderr),
        )
    except (RuntimeError, OSError) as exc:
        raise RuntimeError(
            f"permissions helper is down or refused spawn for session {name!r}; "
            "run `ava converge` and inspect the permissions helper log "
            f"before retrying: {exc}"
        ) from exc
    return result["pid"]


def _wait_until_dead(process: psutil.Process, timeout: float) -> bool:
    """Wait at most ``timeout`` for the captured process identity to stop."""
    if not _process_is_live(process):
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_KILL_POLL_S)
        if not _process_is_live(process):
            return True
    return not _process_is_live(process)


class HelperProcSessionBackend(SessionBackend):
    """Named sessions whose processes are direct children of the macOS helper."""

    def has_session(self, name: str) -> bool:
        record = _read_record(name)
        return record is not None and _process_for_record(record) is not None

    def new_session(
        self,
        name: str,
        cmd: str | list[str],
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
        stderr_append: Path | None = None,
    ) -> bool:
        """Spawn a session through the helper and persist its process identity."""
        if self.has_session(name):
            return True

        if isinstance(cmd, str):
            if login_shell:
                from shared.session_env import exec_into, venv_activation_prefix

                body = exec_into(cmd) if exec_cmd else cmd
                inner = f"cd {cwd.as_posix()} && {venv_activation_prefix()}{body}"
                argv = ["/bin/bash", "-lc", inner]
            else:
                argv = ["/bin/sh", "-c", cmd]
            command_text = cmd
        else:
            argv = list(cmd)
            command_text = " ".join(cmd)

        stdout_path = session_log_path(name)
        stderr_path = stderr_append if stderr_append is not None else stdout_path
        logs_dir().mkdir(parents=True, exist_ok=True)
        child_pid = spawn_via_helper(
            name,
            argv,
            cwd,
            env=env,
            stdout=stdout_path,
            stderr=stderr_path,
        )

        try:
            create_time = psutil.Process(child_pid).create_time()
        except psutil.NoSuchProcess:
            create_time = _DEAD_CHILD_SENTINEL
            starttime = None
        else:
            starttime = pid_starttime_ticks(child_pid)
        SessionRecord(
            pid=child_pid,
            create_time=create_time,
            cmd=command_text,
            cwd=str(cwd),
            started_at=time.time(),
            starttime=starttime,
        ).write(_record_path(name))
        return True

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        """Signal the recorded pid and forget it only after confirmed death."""
        del expected
        record = _read_record(name)
        if record is None:
            return True, "noop"
        process = _process_for_record(record)
        if process is None:
            _record_path(name).unlink(missing_ok=True)
            return True, "noop"

        ended_gracefully = False
        if graceful:
            with contextlib.suppress(*_GONE):
                os.kill(record.pid, signal.SIGTERM)
            ended_gracefully = _wait_until_dead(process, timeout)
        if not ended_gracefully:
            with contextlib.suppress(*_GONE):
                os.kill(record.pid, signal.SIGKILL)
        mode = "graceful" if ended_gracefully else "forced"

        if not ended_gracefully and not _wait_until_dead(process, _KILL_CONFIRM_TIMEOUT_S):
            logger.error(
                "helperproc kill {name}: pid {pid} is still running after the kill — leaving "
                "its session record in place so the survivor stays visible",
                name=name,
                pid=record.pid,
            )
            return False, mode
        _record_path(name).unlink(missing_ok=True)
        return True, mode

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool:
        record = _read_record(name)
        if record is None or (expected is not None and record != expected):
            return False
        process = _process_for_record(record)
        if process is None:
            return False
        with contextlib.suppress(*_GONE):
            if expected is not None and (
                process.create_time() != expected.create_time or _read_record(name) != expected
            ):
                return False
            os.kill(record.pid, signal.SIGTERM)
            return True
        return False

    def list_sessions(self, prefix: str = "") -> list[str]:
        live: list[str] = []
        for record_file in _sessions_dir().glob("*.json"):
            name = record_file.stem
            if prefix and not name.startswith(prefix):
                continue
            record = SessionRecord.read(record_file)
            if record is None:
                record_file.unlink(missing_ok=True)
            elif _process_for_record(record) is not None:
                live.append(name)
            else:
                reapable, reason = _record_reapable(record)
                if not reapable:
                    logger.warning(
                        "helperproc retaining live session record {name}: {reason}",
                        name=name,
                        reason=reason,
                    )
                    continue
                record_file.unlink(missing_ok=True)
        return sorted(live)

    def session_started_at(self, name: str) -> float | None:
        record = _read_record(name)
        if record is None or _process_for_record(record) is None:
            return None
        return record.started_at

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        return {name: self.session_started_at(name) for name in names}

    def session_generation(self, name: str) -> str | None:
        record = _read_record(name)
        if record is None or _process_for_record(record) is None:
            return None
        return record.generation

    def session_log_path(self, name: str) -> Path | None:
        return session_log_path(name)
