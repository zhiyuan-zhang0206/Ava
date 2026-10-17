"""The parent's side of the exec child boundary: the environment it hands the child, and how it
reads the child's outcome back.

Shared by the plain spawn path (`_subprocess.py`) and the admitted-resource path (`_owned_run.py`),
which are siblings: both build the same child environment and map the same result envelope onto
the `ExecResult` sum type, so neither imports the other for it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from base.deploy.release import editable_install
from base.host.env.registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

from ._result import (
    ExecChildError,
    ExecCrashed,
    ExecResult,
    construct_exec_result,
    lifecycle_exception_from_name,
)
from ._stream import StreamCap
from .protocol import ResultPayload, read_result


def _cwd_is_inside_checkout(cwd: Path, checkout_root: Path) -> bool:
    """Whether an exec child cwd may inherit its interpreter's virtualenv."""

    return (
        cwd.is_relative_to(checkout_root)
        and not cwd.is_relative_to(checkout_root / ".worktrees")
        and not cwd.is_relative_to(checkout_root / ".claude" / "worktrees")
    )


def build_child_env(
    agent_id: int | None,
    request_path: Path,
    result_path: Path,
    *,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> dict[str, str]:
    """The child's environment: the parent's own (settings already materialized
    by dotenv_boot), plus the identity the session env allowlist deliberately
    drops (Task #856) and the two envelope paths. Only explicit per-agent maps
    are exported; ambient parent carriers are removed so an unbound child
    cannot inherit another agent's pins (JSON in env, never argv).

    A child spawned outside its interpreter checkout drops ``VIRTUAL_ENV`` so
    a bare uv command cannot target a different checkout's editable install.
    Children running inside their own checkout preserve their venv identity.
    """
    env = os.environ.copy()
    env.pop(AGENT_CONFIG_OVERLAY_ENV, None)
    env.pop(AGENT_BIRTH_CONFIG_ENV, None)
    source_root = editable_install.current_interpreter_source_root()
    if source_root is not None and not _cwd_is_inside_checkout(
        Path.cwd().resolve(), source_root.resolve()
    ):
        env.pop("VIRTUAL_ENV", None)
    if agent_id is not None:
        env["MEIVA_AGENT_ID"] = str(agent_id)
    env["MEIVA_PROCESS_PROFILE"] = "agent"
    env["MEIVA_EXEC_REQUEST_FILE"] = str(request_path)
    env["MEIVA_EXEC_RESULT_FILE"] = str(result_path)
    if config_overlay:
        env[AGENT_CONFIG_OVERLAY_ENV] = json.dumps(config_overlay, sort_keys=True)
    if birth_config:
        env[AGENT_BIRTH_CONFIG_ENV] = json.dumps(birth_config, sort_keys=True)
    return env


def read_result_envelope(
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


def _child_exception(
    payload: ResultPayload | None, envelope_error: str | None
) -> BaseException | None:
    """The exception the child's outcome stands for, or None for a clean exit."""
    if envelope_error is not None:
        return ExecChildError("exec_result_envelope_invalid", envelope_error, None)
    if payload is not None and payload.kind == "lifecycle" and payload.lifecycle_type:
        exc = lifecycle_exception_from_name(payload.lifecycle_type)
        if exc is None:
            exc = ExecChildError(
                "unknown_lifecycle_class",
                "child reported unknown lifecycle class " + repr(payload.lifecycle_type),
                None,
            )
        return exc
    if payload is not None and payload.exc_type:
        # Crash, or a signal exception the agent raised itself
        # (KeyboardInterrupt / TimeoutError written by its own code): if the
        # parent's flags confirm the signal, the construction priority below
        # still wins (cancel/timeout outrank a crash exc).
        return ExecChildError(payload.exc_type, payload.exc_msg or "", payload.full_traceback)
    if payload is None:
        return ExecChildError(
            "exec_subprocess_aborted",
            "the exec child exited without writing a result envelope "
            "(SIGKILLed, watchdog, or the agent's own os._exit)",
            None,
        )
    return None


def result_from_payload(
    output: str,
    payload: ResultPayload | None,
    *,
    cancelled: bool,
    timed_out: bool,
    envelope_error: str | None = None,
    stream_cap: StreamCap | None = None,
    memory_guard_notice: str | None = None,
) -> ExecResult:
    """Map the child's outcome onto the `ExecResult` sum type.

    The parent's cancel/timeout flags stay authoritative (same construction
    priority as the old thread loop); the child's kind is advisory except for
    lifecycle — the envelope carries the `LifecycleExit` class name, which the
    parent reconstructs for the dispatcher's isinstance match. A missing
    envelope with a non-zero exit (agent called os._exit) becomes a crash with
    an explanatory error; when the host memory guard killed the child, that
    error and the agent-visible output state the guard's reason instead.
    """
    if memory_guard_notice is not None and payload is None:
        output = f"{output}\n{memory_guard_notice}\n" if output else f"{memory_guard_notice}\n"
        exc: BaseException | None = ExecChildError(
            "exec_memory_guard_killed", memory_guard_notice, None
        )
    else:
        exc = _child_exception(payload, envelope_error)
    result = construct_exec_result(
        output,
        exc,
        cancelled=cancelled,
        timed_out=timed_out,
        stream_cap=stream_cap,
        code_reached=(payload.code_reached if payload is not None else None),
    )
    if (
        isinstance(result, ExecCrashed)
        and payload is not None
        and payload.full_traceback is not None
    ):
        # The child formatted the full (unfiltered) traceback — the dispatcher
        # logs it instead of formatting the placeholder exception locally.
        result = ExecCrashed(
            output=result.output,
            exc=result.exc,
            full_traceback=payload.full_traceback,
            code_reached=result.code_reached,
            stream_cap=result.stream_cap,
        )
    return result
