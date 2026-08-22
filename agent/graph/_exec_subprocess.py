"""Exec subprocess mechanics — the parent side: spawn / poll / signal / kill /
collect one disposable child per execute_code call.

The parent polls every 50ms and owns teardown through direct-child reap,
root-independent process-domain close, and a bounded output-reader join. POSIX
owns a new process group; Windows owns a Job Object. The child's result
envelope stays advisory except for lifecycle outcomes. It spawns
`python -I -X utf8 -m agent.exec_child`: isolated mode keeps the inherited cwd
and Python environment out of bootstrap import resolution, and explicit UTF-8
mode keeps output portable after isolated mode ignores encoding environment
variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.graph._exec_protocol import (
    KILL_GRACE_S,
    ResultPayload,
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from agent.graph._exec_result import (
    ExecChildError,
    _construct_exec_result,
    _ExecCrashed,
    _ExecResult,
    lifecycle_exception_from_name,
)
from agent.graph._exec_stream import ExecOutputChunkPublisher, StreamCap, StreamingTextIO
from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from shared.paths import exec_run_dir
from shared.platform import CREATE_NO_WINDOW, IS_WINDOWS
from shared.winjob import EXEC_JOB_GATE_ENV, WindowsJob, publish_parent_job_gate

from . import _exec_process

# Same cadence as the old worker-thread poll loop: near-free, and cancel /
# timeout response latency stays <= 50ms + signal delivery.
_POLL_INTERVAL_S = 0.05


def _build_child_env(
    agent_id: int | None,
    request_path: Path,
    result_path: Path,
    *,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
    windows_job_gate: Path | None = None,
) -> dict[str, str]:
    """The child's environment: the parent's own (settings already materialized
    by dotenv_boot), with OTLP export disabled, plus the identity the session env
    allowlist deliberately drops (Task #856) and the two envelope paths. The
    per-agent config maps the agent process popped at boot are re-emitted here so
    the child's SDK calls see the same effective settings (json in env, never
    argv — issue #974)."""
    env = os.environ.copy()
    if agent_id is not None:
        env["AVA_AGENT_ID"] = str(agent_id)
    env["AVA_PROCESS_PROFILE"] = "agent"
    # OTLP export belongs to the long-lived agent process. Avoid first constructing
    # the OTel SDK while this disposable child is in interpreter shutdown.
    env["AVA_TELEMETRY_OTLP_ENABLED"] = "false"
    env["AVA_EXEC_REQUEST_FILE"] = str(request_path)
    env["AVA_EXEC_RESULT_FILE"] = str(result_path)
    if windows_job_gate is not None:
        env[EXEC_JOB_GATE_ENV] = str(windows_job_gate)
    if config_overlay:
        env[AGENT_CONFIG_OVERLAY_ENV] = json.dumps(config_overlay, sort_keys=True)
    if birth_config:
        env[AGENT_BIRTH_CONFIG_ENV] = json.dumps(birth_config, sort_keys=True)
    return env


def _spawn(
    request_path: Path,
    result_path: Path,
    agent_id: int | None,
    *,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
    windows_job_gate: Path | None = None,
) -> tuple[subprocess.Popen[bytes], _exec_process.ExecProcessDomain]:
    """Spawn one child. POSIX raw subprocesses stay in its process group;
    persistent ``ava.shell.sessions`` are backend-hosted and outside it."""
    env = _build_child_env(
        agent_id,
        request_path,
        result_path,
        config_overlay=config_overlay,
        birth_config=birth_config,
        windows_job_gate=windows_job_gate,
    )
    windows_job = WindowsJob.create() if IS_WINDOWS else None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", "-m", "agent.exec_child"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # OS-level merge — preserves print/traceback order
            env=env,
            creationflags=CREATE_NO_WINDOW,
            start_new_session=not IS_WINDOWS,
        )
    except BaseException as original:
        if windows_job is not None:
            _attempt_spawn_cleanup(original, "job_close", windows_job.close)
        raise

    if windows_job is not None:
        if windows_job_gate is None:
            original = RuntimeError("Windows exec spawn requires an attach gate")
            _abort_failed_windows_spawn(proc, windows_job, original)
            raise original
        try:
            windows_job.assign(proc)
            publish_parent_job_gate(windows_job_gate)
        except BaseException as original:
            _abort_failed_windows_spawn(proc, windows_job, original)
            raise
    return proc, _exec_process.ExecProcessDomain(proc=proc, windows_job=windows_job)


def _abort_failed_windows_spawn(
    proc: subprocess.Popen[bytes],
    windows_job: WindowsJob,
    original: BaseException,
) -> None:
    """Fail closed without letting one cleanup failure skip or mask another."""
    _attempt_spawn_cleanup(original, "job_close", windows_job.close)
    _attempt_spawn_cleanup(original, "root_kill", proc.kill)
    _attempt_spawn_cleanup(original, "root_reap", lambda: proc.wait(timeout=5.0))
    if proc.stdout is not None:
        _attempt_spawn_cleanup(original, "stdout_close", proc.stdout.close)


def _attempt_spawn_cleanup(
    original: BaseException,
    stage: str,
    action: Callable[[], object],
) -> None:
    """Attempt one pre-owner cleanup stage, preserving the work failure."""
    try:
        action()
    except Exception as cleanup_error:
        original.add_note(
            "exec spawn cleanup also failed "
            f"({stage}: {type(cleanup_error).__name__}: {cleanup_error})"
        )


def _drain_output(proc: subprocess.Popen[bytes], stream: StreamingTextIO) -> None:
    """Reader thread: pull merged stdout+stderr chunks into the shared stream
    until pipe EOF. Decoded lossily (`errors="replace"`) — the envelope and the
    result path never depend on this decode."""
    stdout = proc.stdout
    assert isinstance(stdout, io.BufferedReader), (  # noqa: S101 — PIPE was requested at spawn
        f"exec child stdout is {type(stdout).__name__}, expected BufferedReader"
    )
    try:
        while True:
            # read1: at most one raw pipe read — returns the bytes available
            # NOW. Plain read(65536) blocks until the buffer fills or EOF,
            # which would hold every chunk back until the child exits and
            # kill live streaming (verified: the first version did exactly that).
            chunk = stdout.read1(65536)
            if not chunk:
                return
            stream.write(chunk.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return  # pipe broken mid-run — whatever was captured stands


async def _poll_child(
    proc: subprocess.Popen[bytes],
    root_exit_task: asyncio.Task[None],
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    timeout: float,
    domain_close: _exec_process.DomainCloseOwner,
) -> tuple[bool, bool]:
    """Poll every 50ms and publish chunks. POSIX signals SIGINT on cancel and
    SIGTERM on deadline; Windows requests immediate Job close. Returns
    (cancelled, timed_out); on a same-tick race cancel wins."""
    deadline = time.monotonic() + timeout
    while not root_exit_task.done():
        # Incrementally publish accumulated stream chunks to frontend.
        if chunk_publisher is not None:
            pending = stream.take_pending()
            if pending:
                chunk_publisher.publish(pending)

        if cancel_event.is_set():
            _exec_process.signal_child(proc, signal.SIGINT, domain_close)
            return True, False
        if time.monotonic() > deadline:
            _exec_process.signal_child(proc, signal.SIGTERM, domain_close)
            return False, True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False, False


async def _collect_child(
    proc: subprocess.Popen[bytes],
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    *,
    cancelled: bool,
    timed_out: bool,
    root_exit_task: asyncio.Task[None],
    reap_task: asyncio.Task[int],
    domain_close: _exec_process.DomainCloseOwner,
    reader_join_task: asyncio.Task[None],
) -> None:
    """Settle every process resource, then publish the final stream chunk."""
    if cancelled or timed_out:
        await _exec_process.wait_with_grace(proc, root_exit_task, KILL_GRACE_S, domain_close)
    failures = await _exec_process.settle_resources(
        root_exit_task,
        reap_task,
        domain_close,
        reader_join_task,
        request_stop=False,
    )
    if failures:
        raise _exec_process.ExecTeardownError(failures)

    if chunk_publisher is not None:
        pending = stream.take_pending()
        if pending:
            chunk_publisher.publish(pending)


async def _finish_failed_run(
    original: BaseException,
    root_exit_task: asyncio.Task[None] | None,
    reap_task: asyncio.Task[int] | None,
    domain_close: _exec_process.DomainCloseOwner | None,
    reader_join_task: asyncio.Task[None] | None,
) -> None:
    """Settle an interrupted run without replacing its primary failure."""
    if root_exit_task is None or reap_task is None or domain_close is None:
        return
    failures = await _exec_process.finish_teardown_despite_cancellation(
        root_exit_task, reap_task, domain_close, reader_join_task
    )
    _exec_process.annotate_original_failure(original, failures)


async def _run_in_subprocess(
    code: str,
    agent_id: int | None,
    cancel_event: asyncio.Event,
    timeout: float,
    chunk_publisher: ExecOutputChunkPublisher | None = None,
    *,
    state: dict[str, Any] | None = None,
    exec_dir: Path | None = None,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> tuple[_ExecResult, ResultPayload | None]:
    """Run `code` in one disposable child process; poll every 50ms monitoring
    exit / cancel_event / deadline (same priority as the old thread loop:
    cancel > deadline > natural completion).

    Returns the `_ExecResult` sum type the exec node dispatches plus the raw
    child envelope (None when the child never wrote one — SIGKILL / watchdog /
    os._exit): the envelope carries the plugin state-update delta and the
    security findings the child drained, which only this function can see.
    The child's outcome kinds map onto the result with the parent's flags
    authoritative.
    """
    if exec_dir is None:
        exec_dir = exec_run_dir()
    request_path = make_request_path(exec_dir, agent_id)
    result_path = make_result_path(exec_dir, agent_id)
    windows_job_gate = request_path.with_suffix(".job-ready.json") if IS_WINDOWS else None

    try:
        write_request(
            request_path,
            code=code,
            agent_id=agent_id,
            timeout_s=timeout,
            state=state,
        )
    except BaseException as exc:
        # State snapshot not serializable (a plugin field type the codec
        # rejects) — fail fast with the channel-level detail, no silent skip.
        return _ExecCrashed(
            output=f"exec subprocess request could not be written: {exc}",
            exc=exc,
        ), None

    stream = StreamingTextIO()
    cancelled = False
    timed_out = False
    proc: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    root_exit_task: asyncio.Task[None] | None = None
    reap_task: asyncio.Task[int] | None = None
    domain_close: _exec_process.DomainCloseOwner | None = None
    reader_join_task: asyncio.Task[None] | None = None
    try:
        try:
            proc, domain = _spawn(
                request_path,
                result_path,
                agent_id,
                config_overlay=config_overlay,
                birth_config=birth_config,
                windows_job_gate=windows_job_gate,
            )
        except OSError as exc:
            return _ExecCrashed(
                output=f"exec subprocess could not be spawned: {exc}",
                exc=exc,
            ), None

        root_exit_task = _exec_process.start_root_exit_observer(proc)
        domain_close = _exec_process.DomainCloseOwner(domain, root_exit_task)
        reap_task = _exec_process.start_reap(proc, domain_close)

        reader = threading.Thread(
            target=_drain_output,
            args=(proc, stream),
            daemon=True,
            name=f"exec-reader-{agent_id}",
        )
        reader.start()

        reader_join_task = _exec_process.start_reader_join(reap_task, reader, proc.pid)

        cancelled, timed_out = await _poll_child(
            proc,
            root_exit_task,
            stream,
            chunk_publisher,
            cancel_event,
            timeout,
            domain_close,
        )
        await _collect_child(
            proc,
            stream,
            chunk_publisher,
            cancelled=cancelled,
            timed_out=timed_out,
            root_exit_task=root_exit_task,
            reap_task=reap_task,
            domain_close=domain_close,
            reader_join_task=reader_join_task,
        )

        payload, envelope_error = _read_result_envelope(result_path, proc.returncode)
        result = _result_from_payload(
            stream.getvalue(),
            payload,
            cancelled=cancelled,
            timed_out=timed_out,
            envelope_error=envelope_error,
            stream_cap=stream.cap(),
        )
        return result, payload
    except asyncio.CancelledError as original:
        # The exec node's outer shield (asyncio.wait_for) cancels this task on
        # node timeout. Cancellation is not complete until every owned resource
        # is settled; otherwise the next exec inherits a zombie/thread leak.
        await _finish_failed_run(
            original, root_exit_task, reap_task, domain_close, reader_join_task
        )
        raise
    except _exec_process.ExecTeardownError:
        raise
    except Exception as original:
        await _finish_failed_run(
            original, root_exit_task, reap_task, domain_close, reader_join_task
        )
        raise
    finally:
        for path in (request_path, result_path, windows_job_gate):
            if path is None:
                continue
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def _read_result_envelope(
    result_path: Path, returncode: int | None
) -> tuple[ResultPayload | None, str | None]:
    """Read the child's result envelope; returns (payload, error).

    `payload` None + `error` None when the envelope is missing (SIGKILL /
    watchdog / the agent's own os._exit — the caller classifies from its own
    flags). A malformed envelope (version drift / unknown kind / oversized) is
    a protocol violation; it comes back as an `error` string so the caller
    surfaces it as a crash outcome instead of guessing "done"."""
    if not result_path.exists():
        if returncode == 124:
            # Watchdog hard-exit (timeout(1) convention) — the parent sent no
            # signal (it was unreachable). Surface as a crash unless the
            # parent's own timeout flag confirms the deadline outcome.
            return (
                ResultPayload(
                    kind="timed_out",
                    exc_type="TimeoutError",
                    exc_msg="exec child watchdog fired — the parent never signalled it",
                ),
                None,
            )
        return None, None
    try:
        return read_result(result_path), None
    except BaseException as exc:
        return None, f"exec result envelope could not be decoded: {exc}"


def _result_from_payload(
    output: str,
    payload: ResultPayload | None,
    *,
    cancelled: bool,
    timed_out: bool,
    envelope_error: str | None = None,
    stream_cap: StreamCap | None = None,
) -> _ExecResult:
    """Map the child's outcome onto the `_ExecResult` sum type.

    The parent's cancel/timeout flags stay authoritative (same construction
    priority as the old thread loop); the child's kind is advisory except for
    lifecycle — the envelope carries the `_LifecycleExit` class name, which the
    parent reconstructs for the dispatcher's isinstance match. A missing
    envelope with a non-zero exit (agent called os._exit) becomes a crash with
    an explanatory error.
    """
    exc: BaseException | None = None
    if envelope_error is not None:
        exc = ExecChildError("exec_result_envelope_invalid", envelope_error, None)
    elif payload is not None and payload.kind == "lifecycle" and payload.lifecycle_type:
        exc = lifecycle_exception_from_name(payload.lifecycle_type)
        if exc is None:
            exc = ExecChildError(
                "unknown_lifecycle_class",
                "child reported unknown lifecycle class " + repr(payload.lifecycle_type),
                None,
            )
    elif payload is not None and payload.exc_type:
        # Crash, or a signal exception the agent raised itself
        # (KeyboardInterrupt / TimeoutError written by its own code): if the
        # parent's flags confirm the signal, the construction priority below
        # still wins (cancel/timeout outrank a crash exc).
        exc = ExecChildError(
            payload.exc_type,
            payload.exc_msg or "",
            payload.full_traceback,
        )
    elif payload is None:
        exc = ExecChildError(
            "exec_subprocess_aborted",
            "the exec child exited without writing a result envelope "
            "(SIGKILLed, watchdog, or the agent's own os._exit)",
            None,
        )
    result = _construct_exec_result(
        output,
        exc,
        cancelled=cancelled,
        timed_out=timed_out,
        stream_cap=stream_cap,
    )
    if (
        isinstance(result, _ExecCrashed)
        and payload is not None
        and payload.full_traceback is not None
    ):
        # The child formatted the full (unfiltered) traceback — the dispatcher
        # logs it instead of formatting the placeholder exception locally.
        result = _ExecCrashed(
            output=result.output,
            exc=result.exc,
            full_traceback=payload.full_traceback,
            stream_cap=result.stream_cap,
        )
    return result


__all__ = ["_build_child_env", "_run_in_subprocess"]
