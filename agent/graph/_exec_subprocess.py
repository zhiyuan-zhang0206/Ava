"""Exec subprocess mechanics — the parent side: spawn / poll / signal / kill /
collect one disposable child per execute_code call.

Model (`python -m agent.exec_child`, same venv, `start_new_session=True` so a
process group exists to kill): the parent stays authoritative on
cancel/timeout — 50ms cadence, SIGINT for cancel, SIGTERM for timeout,
SIGKILL(-pgid) after a grace period — and the child's envelope kind is
advisory except for lifecycle outcomes (only the child can know which
`_LifecycleExit` subclass ran). Output streams back through the merged pipe
into a `StreamingTextIO`, so chunk publishing to the frontend is unchanged.
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
from shared.log import logger
from shared.paths import exec_run_dir
from shared.platform import CREATE_NO_WINDOW, IS_WINDOWS

# Same cadence as the old worker-thread poll loop: near-free, and cancel /
# timeout response latency stays <= 50ms + signal delivery.
_POLL_INTERVAL_S = 0.05

# How long the reader thread may lag behind the child's exit (a grandchild
# holding fd 1 open delays pipe EOF). Bounded so a stuck reader never blocks
# the result path; the thread is daemon and dies with the agent process.
_READER_JOIN_TIMEOUT_S = 5.0


def _build_child_env(
    agent_id: int | None,
    request_path: Path,
    result_path: Path,
    *,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> dict[str, str]:
    """The child's environment: the parent's own (settings already materialized
    by dotenv_boot), plus the identity the session env allowlist deliberately
    drops (Task #856) and the two envelope paths. The per-agent config maps the
    agent process popped at boot are re-emitted here so the child's SDK calls
    see the same effective settings (json in env, never argv — issue #974)."""
    env = os.environ.copy()
    if agent_id is not None:
        env["AVA_AGENT_ID"] = str(agent_id)
    env["AVA_PROCESS_PROFILE"] = "agent"
    env["AVA_EXEC_REQUEST_FILE"] = str(request_path)
    env["AVA_EXEC_RESULT_FILE"] = str(result_path)
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
) -> subprocess.Popen[bytes]:
    """Spawn the exec child. `start_new_session=True` puts it in its own
    process group (pgid = pid), so cancel/timeout can kill the whole tree —
    grandchildren the agent started through ava.shell die with it."""
    env = _build_child_env(
        agent_id,
        request_path,
        result_path,
        config_overlay=config_overlay,
        birth_config=birth_config,
    )
    return subprocess.Popen(
        [sys.executable, "-m", "agent.exec_child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # OS-level merge — preserves print/traceback order
        env=env,
        creationflags=CREATE_NO_WINDOW,
        start_new_session=not IS_WINDOWS,
    )


def _signal_child(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the child's process group (POSIX); Windows has no process groups
    — terminate the process itself (best-effort, agent-runner-only platform)."""
    if IS_WINDOWS:
        proc.terminate()
        return
    # Already-gone races the poll loop observes as exit — suppress, don't log.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def _kill_child(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the whole group — the guarantee the thread model never had: a
    native-stuck child (or one swallowing every exception) dies regardless."""
    if IS_WINDOWS:
        proc.kill()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _wait_with_grace(proc: subprocess.Popen[bytes], grace_s: float) -> bool:
    """Wait for the child up to `grace_s` (letting its KeyboardInterrupt /
    TimeoutError handler write the envelope + unwind); return True if it exited
    within the grace period. Past it, SIGKILL the group and wait for reaping."""
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=grace_s)
        return True
    except TimeoutError:
        _kill_child(proc)
        logger.warning(
            "[{label}] exec child {pid} survived the {grace}s grace period — "
            "SIGKILLed the process group (native-stuck code or a swallowed "
            "signal; this is the kill the thread model could not perform)",
            label="exec-subprocess-killed",
            pid=proc.pid,
            grace=grace_s,
            event="exec_subprocess_killed",
        )
        await asyncio.to_thread(proc.wait)
        return False


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
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    timeout: float,
) -> tuple[bool, bool]:
    """Poll the child every 50ms: publish accumulated stream chunks, SIGINT on
    cancel (user Stop), SIGTERM on deadline. Returns (cancelled, timed_out); on
    a same-tick race cancel wins (priority enforced by the result
    construction)."""
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        # Incrementally publish accumulated stream chunks to frontend.
        if chunk_publisher is not None:
            pending = stream.take_pending()
            if pending:
                chunk_publisher.publish(pending)

        if cancel_event.is_set():
            _signal_child(proc, signal.SIGINT)
            return True, False
        if time.monotonic() > deadline:
            _signal_child(proc, signal.SIGTERM)
            return False, True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False, False


async def _collect_child(
    proc: subprocess.Popen[bytes],
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    reader: threading.Thread,
    *,
    cancelled: bool,
    timed_out: bool,
) -> None:
    """Reap the child (grace period then SIGKILL for a signalled exit),
    drain the reader to EOF, and publish the final stream increment."""
    if cancelled or timed_out:
        await _wait_with_grace(proc, KILL_GRACE_S)
    else:
        await asyncio.to_thread(proc.wait)  # natural exit — reap

    # Give the reader the EOF flush; a grandchild still holding fd 1 open
    # can delay EOF past the child's exit — bounded, never blocking, and never
    # raising into the caller (the output captured so far stands).
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(reader.join), timeout=_READER_JOIN_TIMEOUT_S)

    if chunk_publisher is not None:
        pending = stream.take_pending()
        if pending:
            chunk_publisher.publish(pending)


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
    try:
        try:
            proc = _spawn(
                request_path,
                result_path,
                agent_id,
                config_overlay=config_overlay,
                birth_config=birth_config,
            )
        except OSError as exc:
            return _ExecCrashed(
                output=f"exec subprocess could not be spawned: {exc}",
                exc=exc,
            ), None

        reader = threading.Thread(
            target=_drain_output,
            args=(proc, stream),
            daemon=True,
            name=f"exec-reader-{agent_id}",
        )
        reader.start()

        cancelled, timed_out = await _poll_child(
            proc, stream, chunk_publisher, cancel_event, timeout
        )
        await _collect_child(
            proc, stream, chunk_publisher, reader, cancelled=cancelled, timed_out=timed_out
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
    except asyncio.CancelledError:
        # The exec node's outer shield (asyncio.wait_for) cancels this task on
        # node timeout — the child must not survive its parent's decision.
        if proc is not None and proc.poll() is None:
            _kill_child(proc)
        raise
    finally:
        for path in (request_path, result_path):
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
