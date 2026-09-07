"""Exec subprocess entry — `python -I -X utf8 -m agent.exec_child`.

Each execute_code call runs here, in a fresh process the exec node spawns, so
a stuck native call (numpy / ctypes / an `except BaseException` swallow loop)
can be SIGKILLed without touching the agent process — issue #184. The agent
process stays alive; this child is disposable.

Contract with the parent (`agent/graph/_exec_subprocess.py`), all through
files + signals:

- Request envelope: `AVA_EXEC_REQUEST_FILE` — the code, the agent id, the
  timeout, and the typed state snapshot (`agent/graph/_exec_protocol.py`).
- Output: fd 1/2, merged into one pipe by the parent (`stderr=STDOUT`). Only
  the agent's own output goes there: framework logs use the file sink
  (`init_subprocess_logger` adds no stderr handler), and stdout/stderr are
  reconfigured to line buffering so `print(..., end="")` still streams.
- Result envelope: `AVA_EXEC_RESULT_FILE` — outcome kind, plugin state-update
  delta, security findings, attachments, and (for a crash) the full traceback
  text. Written on every exit path except `os._exit` (watchdog / the agent's
  own call) and SIGKILL — the parent classifies those from its own
  cancel/timeout flags.
- POSIX signals: SIGINT -> KeyboardInterrupt, SIGTERM -> TimeoutError, both raised
  at the next bytecode boundary (the same semantics the old in-thread ctypes
  injection had). POSIX gets a grace period before the parent closes the
  process group; Windows cancel/timeout immediately closes the Job Object;
  a watchdog `os._exit(124)` bounds this child's life if the parent dies first.

Identity: `ava._boot.establish(agent_id, owns_loop=True)` — owns_loop stays
True so `ava.self.terminate/restart/compact` keep working exactly as they do
in the agent process (their inbound INSERTs go to the same database over
`ava.DB`); the resulting `_LifecycleExit` is caught here and reported as a
lifecycle outcome. The parent reconstructs the exception from the name
(`agent.graph._exec_result.lifecycle_exception_from_name`).

Per-agent config: the host exports its bound framework and plugin pins through
`AVA_AGENT_CONFIG_OVERLAY`. Direct embedding callers may also pass
`AVA_AGENT_BIRTH_CONFIG`; overlay values take precedence. The child removes
both carriers from its environment before applying framework configuration,
then applies plugin configuration after plugin loading. SDK calls made from
exec code see their owning turn's effective settings.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Literal, cast

# isort: split
# First import after stdlib, BEFORE the heavy `import ava` inside `_run`:
# importing shared.log runs `logger.remove()` (dropping loguru's default
# stderr handler). Without this, a Settings-construction warning that fires
# during the ava import chain (e.g. `_warn_when_timezone_unset` on a host
# without AVA_TIMEZONE — shared/config/general.py logs it on loguru directly)
# lands on stderr, which the parent pipes straight into the agent's exec
# output. CI caught this leak twice (2026-08-21, PR #256 shard 1): once
# unfixed, once after the import sorter silently moved the guard below the
# heavy import — the split markers above and below pin the order.
import shared.log  # noqa: F401  # pyright: ignore[reportUnusedImport]  # side effect is the point

# isort: split
from shared.log import init_subprocess_logger, logger
from shared.winjob import EXEC_JOB_GATE_ENV, await_parent_job_gate

# Covers child runtime setup after initial module imports, ending immediately
# before agent-authored code begins. The parent-owned exec duration includes
# this interval but cannot isolate it from user code.
_CHILD_BOOT_STARTED_AT = time.perf_counter()

# Watchdog margin beyond (timeout + parent's kill grace) before the child
# hard-exits — overridable so tests do not wait for the 5s default.
WATCHDOG_MARGIN_S = 5.0

# Exit code the watchdog uses (the `timeout(1)` convention, same as watchers).
WATCHDOG_EXIT_CODE = 124

_RESULT_KIND: dict[type[BaseException], Literal["cancelled", "timed_out"]] = {
    KeyboardInterrupt: "cancelled",
    TimeoutError: "timed_out",
}


def _import_runtime() -> None:
    """Load the SDK only after `main` can turn a boot failure into a result."""
    global ava, _boot  # noqa: PLW0603

    import ava
    from ava import _boot


def _line_buffered_output() -> None:
    """stdout/stderr are pipes; default buffering would hold output until the
    buffer fills. Line buffering restores the live-streaming the old
    in-thread capture gave (a print with no newline still shows up)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def _install_signal_handlers() -> None:
    """SIGINT -> KeyboardInterrupt, SIGTERM -> TimeoutError.

    The parent signals cancel with SIGINT and timeout with SIGTERM; both must
    surface as catchable exceptions inside the agent's code so `finally`
    blocks run and the result envelope is still written. If the agent
    overwrites the handlers or is stuck in native code, the parent escalates
    to SIGKILL after the grace period — that is the guarantee this whole
    design exists for.
    """

    def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def _raise_timeout_error(_signum: int, _frame: object) -> None:
        raise TimeoutError("exec subprocess timed out")

    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, _raise_timeout_error)


def _arm_watchdog(timeout_s: float) -> None:
    """Hard-exit past (timeout + parent kill grace + margin) — the belt to the
    parent's braces. Only fires when the parent itself died (or its signals
    were lost); a parent that is alive SIGKILLs this child first."""
    from agent.graph._exec_protocol import KILL_GRACE_S

    margin = float(os.environ.get("AVA_EXEC_WATCHDOG_MARGIN_S", WATCHDOG_MARGIN_S))
    delay = timeout_s + KILL_GRACE_S + margin

    def _timeout() -> None:
        os._exit(WATCHDOG_EXIT_CODE)

    timer = threading.Timer(delay, _timeout)
    # Daemon: a pending watchdog must never keep the interpreter alive after
    # the exec finished (the child would otherwise sit idle until the timer
    # fires — the first version of this module did exactly that).
    timer.daemon = True
    timer.start()


def _pop_overlay_env() -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Pop + JSON-decode the re-emitted per-agent config maps, exactly once —
    applying them in two phases (framework now, plugin after plugins load)
    must not re-read the env."""
    import json as _json

    from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

    maps: dict[str, dict[str, object] | None] = {}
    for env_name in (AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV):
        raw = os.environ.pop(env_name, "")
        if not raw:
            maps[env_name] = None
            continue
        value = _json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError(f"{env_name} must be a JSON object, got {type(value).__name__}")
        maps[env_name] = cast(dict[str, object], value)
    return (
        maps[AGENT_BIRTH_CONFIG_ENV],
        maps[AGENT_CONFIG_OVERLAY_ENV],
    )


def _apply_overlay_scope(
    birth: dict[str, object] | None,
    overlay: dict[str, object] | None,
    *,
    scope: Literal["framework", "plugin"],
) -> bool:
    """Apply both maps at one scope — birth first, overlay on top (the same
    precedence the host uses when it resolves stored configuration).
    Returns True when at least one map applied."""
    from shared.plugin_config_registry import apply_config_overlay

    applied = False
    for value in (birth, overlay):
        if value:
            apply_config_overlay(value, scope=scope)
            applied = True
    return applied


def _init_logger(agent_id: int | None) -> None:
    """File sink only, plus a best-effort event-pipeline sink for sdk_call
    events. The pipeline open is best-effort here — a DB outage must not stop
    agent code from running (unlike the agent process, which fails loud at
    boot). A failure degrades to the file sink with a warning."""
    if agent_id is None:
        return
    init_subprocess_logger(agent_id=agent_id)
    try:
        from shared.log import _add_postgres_sink

        _add_postgres_sink(process="agent-exec", agent_id=agent_id)
    except Exception:
        logger.warning(
            "[exec-child] event pipeline sink unavailable — sdk_call events "
            "for this exec reach the file sink only",
            agent_id=agent_id,
        )


def _emit_child_boot_timing() -> None:
    """Record the child-ready boundary before executing agent-authored code."""
    duration_ms = (time.perf_counter() - _CHILD_BOOT_STARTED_AT) * 1000
    logger.info(
        "exec child boot completed in {duration_ms:.1f}ms",
        event="exec_child_boot",
        duration_ms=duration_ms,
    )


def _build_state_slot(state: dict[str, Any] | None) -> None:
    """Rebuild the dynamic AgentState class in this process and inject the
    snapshot into the plugin state slots (ava.state / ava.state_update)."""
    if state is None:
        return
    from agent.state import build_agent_state

    state_cls = build_agent_state()
    ava.state = state_cls.model_validate(state)
    ava.state_update = {}


def _take_result_state_update(payload: Any, *, state_injected: bool) -> None:
    """Serialize this turn's plugin delta into the result envelope.

    A tampered slot (agent set ava.state_update to a non-dict) is reported as
    an error string rather than a delta; the parent raises the same TypeError
    the old in-process path raised. With a snapshot injected, even None is
    tampering — the slot was initialized to {}; without one (container/eval
    mode) None is the uninitialized default and carries no delta.
    """
    update = ava.state_update
    if update is None:
        if state_injected:
            payload.state_update_error = (
                "plugin tampered with ava.state_update: expected dict, got NoneType"
            )
        return
    if not isinstance(update, dict):
        payload.state_update_error = (
            f"plugin tampered with ava.state_update: expected dict, got {type(update).__name__}"
        )
        return
    payload.state_update = update


def _run_code(code: str, payload: Any) -> None:
    """Execute the agent's code with stdout/stderr on the pipe; capture
    lifecycle / crash outcomes into the payload."""
    from agent.graph._agent_traceback import (
        format_agent_traceback,
        format_full_traceback,
        register_agent_source,
    )
    from ava._exports.help import HelpRouter
    from shared import sdk_telemetry

    # Register the source so `<agent_code>` frames resolve their offending
    # line in tracebacks (exec'd code is invisible to linecache).
    register_agent_source(code)
    builtins_source = (
        cast(dict[str, Any], __builtins__)
        if isinstance(__builtins__, dict)
        else cast(dict[str, Any], vars(__builtins__))
    )
    # The module is process-global; only this exec's copied binding may change.
    builtins_map = dict(builtins_source)
    original_help = builtins_map["help"]
    builtins_map["help"] = HelpRouter(original_help)
    fresh_globals: dict[str, Any] = {
        "__name__": "__agent_code__",
        "__builtins__": builtins_map,
    }
    try:
        # `recording()` arms SDK-usage metering for exactly this agent-authored
        # code, so framework-internal ava.* calls are never counted (same
        # contract as the old in-process worker had).
        with sdk_telemetry.recording():
            exec(compile(code, "<agent_code>", "exec"), fresh_globals)
    except BaseException as exc:
        from shared.lifecycle import _LifecycleExit

        if isinstance(exc, _LifecycleExit):
            # Lifecycle (terminate/restart/compact): SDK already INSERTed the
            # inbound; no traceback (not an error).
            payload.kind = "lifecycle"
            payload.lifecycle_type = type(exc).__name__
            return
        # Ordinary exceptions / SystemExit / KeyboardInterrupt (cancel signal)
        # / TimeoutError (timeout signal). Only the agent's own `<agent_code>`
        # frames go to the pipe (agent-facing surface); the full traceback
        # rides the envelope for the parent's logs. The exc details are
        # recorded for the signal kinds too: when the parent's flags do not
        # confirm (the agent raised KeyboardInterrupt itself), the parent
        # maps the envelope back to a crash, not a clean done.
        payload.kind = _RESULT_KIND.get(type(exc), "crashed")
        payload.exc_type = type(exc).__name__
        payload.exc_msg = str(exc)[:2000]
        payload.full_traceback = format_full_traceback(exc)
        sys.stdout.write(format_agent_traceback(exc))
        sys.stdout.flush()


def _run(request_path: str, result_path: str) -> None:
    """Child body: read the request, set up identity + plugins + state, run the
    code, write the result envelope."""
    _import_runtime()
    from agent.graph._exec_protocol import ResultPayload, read_request, write_result
    from ava._attach import media_gated_members, take_attachments
    from ava._exports.discovery import _hidden_surface_members
    from ava.security import take_findings

    _line_buffered_output()
    _install_signal_handlers()
    if "AVA_AGENT_ID" in os.environ:
        _init_logger(int(os.environ["AVA_AGENT_ID"]))
    request = read_request(Path(request_path))
    payload = ResultPayload(kind="done")

    birth, overlay = _pop_overlay_env()
    if request.agent_id is not None:
        _boot.establish(request.agent_id, owns_loop=True)
        if request.incarnation is not None:
            from shared.runtime_incarnation import bind_child_incarnation

            bind_child_incarnation(request.incarnation)
        _init_logger(request.agent_id)
        from shared import telemetry_otlp

        telemetry_otlp.warmup()
    # Two-phase overlay application, mirroring the agent process's own boot:
    # framework fields early (before any settings read), plugin fields after
    # plugins load (apply_config_overlay needs _PLUGIN_CONFIGS bound first).
    if _apply_overlay_scope(birth, overlay, scope="framework"):
        # Per-agent sdk_disable additions ride the overlay; re-apply on top of
        # the env baseline (idempotent — only new entries take effect).
        from agent._process_boot import _apply_per_agent_sdk_disable

        _apply_per_agent_sdk_disable()
    # A text-only agent gets no attach contract anywhere in its SDK docs —
    # including interactive `ava.help(ava.self)` (user ruling 2026-08-28).
    # Set for the child's whole lifetime; the token is deliberately held.
    _hidden_surface_members.set(media_gated_members())
    # Load plugin namespaces (ava.tasks etc.) + wraps + state fields into this
    # process — the same explicit load a watcher child runs. Idempotent.
    ava._ensure_plugins_loaded()
    _apply_overlay_scope(birth, overlay, scope="plugin")
    from agent._process_boot import _apply_per_agent_eval_isolation

    _apply_per_agent_eval_isolation()
    _build_state_slot(request.state)

    if request.timeout_s > 0:
        _arm_watchdog(request.timeout_s)

    try:
        _emit_child_boot_timing()
        _run_code(request.code, payload)
        # Deliver queued SDK-call events before a clean exit. sync() lands the
        # pipeline's held batch (queue + drain-thread batch), flush() then
        # drains the OTLP backend queue and force-flushes the SDK batch
        # processor — a short-lived child exits before the 5s batch window
        # would fire on its own. A timed-out or cancelled child skips this
        # (the parent is already killing it and the JSONL mirror holds the
        # records), so teardown timing stays as before warmup().
        if payload.kind in ("done", "lifecycle"):
            from shared import telemetry, telemetry_otlp

            telemetry.sync()
            telemetry_otlp.flush()
    finally:
        _take_result_state_update(payload, state_injected=request.state is not None)
        payload.findings = [f.model_dump() for f in take_findings()]
        payload.attachments = take_attachments()
        try:
            write_result(Path(result_path), payload)
        except BaseException as write_exc:
            _write_crashed_result(result_path, write_exc)


def main() -> None:
    """Entry: run one exec and exit 0 — the result envelope carries the
    semantics, not the exit code (a non-zero exit would add nothing the
    envelope does not already say, and the parent treats a missing envelope
    as the crash path anyway)."""
    await_parent_job_gate(os.environ.get(EXEC_JOB_GATE_ENV))
    request_path = os.environ.get("AVA_EXEC_REQUEST_FILE")
    result_path = os.environ.get("AVA_EXEC_RESULT_FILE")
    if not request_path or not result_path:
        sys.stderr.write(
            "agent.exec_child needs AVA_EXEC_REQUEST_FILE and AVA_EXEC_RESULT_FILE "
            "in the environment — spawn it via agent.graph._exec_subprocess\n"
        )
        raise SystemExit(2)
    try:
        _run(request_path, result_path)
    except BaseException as exc:
        _write_crashed_result(result_path, exc)


def _write_crashed_result(result_path: str, exc: BaseException) -> None:
    """Best-effort crash envelope that cannot itself hide the original failure."""
    exc_type = type(exc).__name__
    exc_msg = str(exc)[:2000]
    full_traceback = _format_current_traceback(exc)
    envelope: dict[str, object] = {
        "v": 1,
        "kind": "crashed",
        "lifecycle_type": None,
        "exc_type": exc_type,
        "exc_msg": exc_msg,
        "full_traceback": full_traceback,
        "state_update_error": None,
        "findings": None,
        "attachments": None,
    }
    try:
        from agent.graph._exec_protocol import ResultPayload as RuntimeResultPayload
        from agent.graph._exec_protocol import write_result as runtime_write_result

        runtime_write_result(
            Path(result_path),
            RuntimeResultPayload(
                kind="crashed",
                exc_type=exc_type,
                exc_msg=exc_msg,
                full_traceback=full_traceback,
            ),
        )
        return
    except BaseException:
        _write_crashed_result_stdlib(result_path, envelope)


def _write_crashed_result_stdlib(result_path: str, envelope: dict[str, object]) -> None:
    """Write the minimal result shape without importing application modules."""
    path = Path(result_path)
    fd: int | None = None
    try:
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        result_file = os.fdopen(fd, "wb")
        fd = None
        with result_file:
            result_file.write(data)
    except BaseException as write_exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(BaseException):
            sys.stderr.write(
                "exec child could not write crash result envelope: "
                f"{type(write_exc).__name__}: {write_exc}\n"
            )
            sys.stderr.flush()


def _format_current_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


if __name__ == "__main__":
    main()
