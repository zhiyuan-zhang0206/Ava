"""Exec subprocess entry — `python -I -X utf8 -m agent.execution.child`.

Each execute_code call runs here, in a fresh process the exec node spawns, so
a stuck native call (numpy / ctypes / an `except BaseException` swallow loop)
can be SIGKILLed without touching the agent process — issue #184. The agent
process stays alive; this child is disposable.

Contract with the parent (`agent/graph/exec/_subprocess.py`), all through
files + signals:

- Request envelope: `AVA_EXEC_REQUEST_FILE` — the code, the agent id, the
  timeout, and the typed state snapshot (`agent/graph/exec/protocol.py`).
- Output: fd 1/2, merged into one pipe by the parent (`stderr=STDOUT`). Only
  the agent's own output goes there: framework logs use the file sink
  (`init_subprocess_logger` adds no stderr handler), and stdout/stderr are
  reconfigured to line buffering so `print(..., end="")` still streams.
- Result envelope: `AVA_EXEC_RESULT_FILE` — outcome kind, plugin state-update
  delta (plugin fields, security findings, attachment registrations), the run's SDK-call
  tally, and (for a crash) the full traceback text. Written on every exit path except `os._exit`
  (watchdog / the agent's own call) and SIGKILL — the parent classifies those
  from its own cancel/timeout flags.
- POSIX signals: SIGINT -> KeyboardInterrupt, SIGTERM -> TimeoutError, both raised
  at the next bytecode boundary (the same semantics the old in-thread ctypes
  injection had). The parent allows a grace period before closing the
  process group;
  a watchdog `os._exit(124)` bounds this child's life if the parent dies first.

Context: the request envelope carries the description of the host's `AvaContext`
(`AvaContext.describe`); the child builds its own instance from it
(`AvaContext.from_description`) and installs it in the child-local `ava.context`, so
agent code reads it as `ava.context`. The identity carries owns_loop=True, so
`ava.self.terminate/restart/compact` keep working exactly as they do
in the agent process (their inbound INSERTs go to the same database over
`ava.DB`); the resulting `LifecycleExit` is caught here and reported as a
lifecycle outcome. The parent reconstructs the exception from the name
(`agent.graph.exec._result.lifecycle_exception_from_name`).

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

# isort: split
# First import after stdlib, BEFORE the heavy `import ava` inside `_run`:
# importing base.log runs `logger.remove()` (dropping loguru's default
# stderr handler). Without this, a Settings-construction warning that fires
# during the ava import chain (e.g. `_warn_when_timezone_unset` on a host
# without AVA_TIMEZONE — base/config/domains/general.py logs it on loguru directly)
# lands on stderr, which the parent pipes straight into the agent's exec
# output. CI caught this leak twice (2026-08-21, PR #256 shard 1): once
# unfixed, once after the import sorter silently moved the guard below the
# heavy import — the split markers above and below pin the order.
import base.log  # noqa: F401  # pyright: ignore[reportUnusedImport]  # side effect is the point

# isort: split
# Envelope types the boot path references: a leaf module (stdlib + small shared
# helpers, no serde — `loads_typed` stays deferred), so importing it here keeps
# the child's early-import order intact.
from agent.graph.exec.protocol import RequestPayload
from base.log import init_subprocess_logger, logger

# Watchdog margin beyond (timeout + parent's kill grace) before the child
# hard-exits — overridable so tests do not wait for the 5s default.
WATCHDOG_MARGIN_S = 5.0

# Exit code the watchdog uses (the `timeout(1)` convention, same as watchers).
WATCHDOG_EXIT_CODE = 124

_RESULT_KIND: dict[type[BaseException], Literal["cancelled", "timed_out"]] = {
    KeyboardInterrupt: "cancelled",
    TimeoutError: "timed_out",
}


class _Sdk(Protocol):
    """The slice of the `ava` module the child drives: the framework-internal state slots and
    the plugin load."""

    state: Any
    state_update: dict[str, Any] | None

    def ensure_plugins_loaded(
        self,
        *,
        surface: bool = True,
        config: Any = None,
        clock_factory: Callable[[], Any] | None = None,
        producer: Callable[[], Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class _ChildContext:
    """What this one exec child holds for the run: the SDK modules it bound after boot, and
    when boot began.

    `boot_started_at` covers child runtime setup after the initial module imports, ending
    immediately before agent-authored code begins; the parent-owned exec duration includes
    this interval but cannot isolate it from user code.
    """

    ava: _Sdk
    boot_started_at: float


def _import_runtime(boot_started_at: float) -> _ChildContext:
    """Load the SDK only after `main` can turn a boot failure into a result."""
    import ava

    return _ChildContext(ava=ava, boot_started_at=boot_started_at)


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
    from base.native_process.exec_domain import KILL_GRACE_S

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

    from base.host.env.registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

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
    set_framework_field: Callable[[str, object], None] | None = None,
) -> bool:
    """Apply both maps at one scope — birth first, overlay on top (the same
    precedence the host uses when it resolves stored configuration).
    Returns True when at least one map applied."""
    applied = False
    for value in (birth, overlay):
        if value:
            if scope == "framework":
                from base.packages.plugins.config_registration import apply_config_overlay

                apply_config_overlay(value, scope=scope, set_framework_field=set_framework_field)
            else:
                from ava.sdk_surface import install

                install.apply_config_overlay(value)
            applied = True
    return applied


def _init_logger(
    agent_id: int | None,
    *,
    producer: Callable[[], Any],
    machine_reader: Callable[[], str],
) -> None:
    """File sink only, plus a best-effort event-pipeline sink for sdk_call
    events. The pipeline open is best-effort here — a DB outage must not stop
    agent code from running (unlike the agent process, which fails loud at
    boot). A failure degrades to the file sink with a warning.

    With the sink in place, the boot timing line becomes the child's first
    event-pipeline record, so the OTLP side is armed for deferred export here,
    before any record can flow (task #3816 M4b; see
    `base.telemetry.otlp.telemetry_otlp_defer`). A failed sink registration skips the arm."""
    if agent_id is None:
        return
    init_subprocess_logger(agent_id=agent_id)
    try:
        from base.log import add_postgres_sink

        add_postgres_sink(
            process="agent-exec",
            agent_id=agent_id,
            producer=producer,
            machine_reader=machine_reader,
        )
    except Exception:
        logger.warning(
            "[exec-child] event pipeline sink unavailable — sdk_call events "
            "for this exec reach the file sink only",
            agent_id=agent_id,
        )
        return
    from base.telemetry.otlp import telemetry_otlp

    telemetry_otlp.defer_until_exit()


def _emit_child_boot_timing(child: _ChildContext) -> None:
    """Record the child-ready boundary before executing agent-authored code."""
    duration_ms = (time.perf_counter() - child.boot_started_at) * 1000
    extra: dict[str, object] = {}
    module = sys.modules.get("base.telemetry.otlp.telemetry_otlp")
    if module is not None and hasattr(module, "deferred_state"):
        # Diagnostic marker (task #3816 M4b): held for deferred export?
        extra["otlp_deferred"] = module.deferred_state()
    logger.info(
        "exec child boot completed in {duration_ms:.1f}ms",
        event="exec_child_boot",
        duration_ms=duration_ms,
        **extra,
    )


class _LazyStateSlot:
    """State slot for a stateful request — materializes on first use.

    Holds the request's raw snapshot; `materialize()` loads the plugin faces
    (state-field registrations), decodes the typed blob, rebuilds and validates
    the dynamic AgentState, then swaps itself out of `ava.state`. Attribute
    access before and after goes through (materialize-then-forward), so plugin
    handles (`PluginStateHandle.read/update`) and `ava.state.<x>` reads work
    unchanged — to the outside the slot behaves like the state object (task
    #3633 leg-2: the child start must not pay the serde + `agent.state` +
    graph/LM stack for a snapshot the turn may never touch).
    """

    def __init__(self, payload: RequestPayload, ava: _Sdk) -> None:
        object.__setattr__(self, "_payload", payload)
        object.__setattr__(self, "_ava", ava)
        object.__setattr__(self, "_real", None)

    def materialize(self) -> None:
        """Resolve the slot once: faces, then decode + validate, then swap."""
        if object.__getattribute__(self, "_real") is not None:
            return
        # The faces declare the plugins' state fields — loaded before the registry is built, and the
        # dynamic class is built before the blob is decoded: the decode's allowlist is the class's.
        ava: _Sdk = object.__getattribute__(self, "_ava")
        ava.ensure_plugins_loaded(surface=False)
        payload: RequestPayload = object.__getattribute__(self, "_payload")
        from agent.extensions.registry import build_registry
        from agent.state import build_agent_state

        state_cls = build_agent_state(build_registry())
        snapshot = payload.materialize_state()
        real = state_cls.model_validate(snapshot)
        object.__setattr__(self, "_real", real)
        ava.state = real

    def __getattr__(self, name: str) -> Any:
        self.materialize()
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        self.materialize()
        setattr(object.__getattribute__(self, "_real"), name, value)


def _build_state_slot(child: _ChildContext, payload: RequestPayload) -> None:
    """Arm the dynamic AgentState slot from the request snapshot.

    A stateless request binds no slot, so `ava.state` does not exist in that child. A
    stateful request binds a lazy slot (task #3633 leg-2): the decode, the plugin faces,
    and the dynamic-class build/validation happen on first use — a handle call or an
    `ava.state.<x>` read (`_LazyStateSlot.materialize`).
    """
    if payload.state_raw is None:
        return
    child.ava.state = _LazyStateSlot(payload, child.ava)
    child.ava.state_update = {}


def _take_result_state_update(child: _ChildContext, payload: Any, *, state_injected: bool) -> None:
    """Serialize this turn's plugin delta into the result envelope.

    A tampered slot (agent replaced ava.state_update with a non-dict) is reported as
    an error string rather than a delta; the parent raises the same TypeError
    the old in-process path raised. A request without a snapshot (container/eval
    mode) bound no slot, so there is nothing to read and no delta to carry.
    """
    if not state_injected:
        return
    update = child.ava.state_update
    if not isinstance(update, dict):
        payload.state_update_error = (
            f"plugin tampered with ava.state_update: expected dict, got {type(update).__name__}"
        )
        return
    # `{}` = the slot was armed but never touched: no delta to send, and the
    # exit path must not pay the serde for it (task #3633 leg-2).
    payload.state_update = update or None


def _run_code(code: str, payload: Any) -> None:
    """Execute the agent's code with stdout/stderr on the pipe; capture
    lifecycle / crash outcomes into the payload."""
    from agent.graph.agent_traceback import (
        format_agent_traceback,
        format_full_traceback,
        register_agent_source,
    )
    from ava.sdk_surface import install as sdk_install
    from ava.sdk_surface.help import HelpRouter
    from base.agents.sdk import telemetry as sdk_usage_telemetry

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
    import ava

    tally = sdk_usage_telemetry.SdkCallTally()
    execution_context = ava.context
    installation = sdk_install.installed()
    sampling = (
        contextlib.nullcontext() if installation is None else installation.sampling.execution()
    )
    ava.bind_context(replace(execution_context, sdk_calls=tally))
    try:
        # From here on the agent-authored code has run (or is about to) — the
        # envelope flag the parent uses to tell "the code never executed" from
        # "it executed and printed nothing" (P0 #2100).
        payload.code_reached = True
        # Each child owns one execution tally. Its context shares that owner with
        # all public SDK entries, including ordinary threads; boot calls occurred
        # before this binding and do not enter the execution's result.
        with sampling:
            exec(compile(code, "<agent_code>", "exec"), fresh_globals)
    except BaseException as exc:
        from base.agents.lifecycle import LifecycleExit

        if isinstance(exc, LifecycleExit):
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
    finally:
        payload.sdk_calls = sdk_usage_telemetry.tally_entries(tally.snapshot())
        ava.bind_context(execution_context)


def _finalize_telemetry(*, clients: Any = None) -> None:
    """Attempt finite ordinary delivery in life, then flush the available OTLP tail.

    A completed barrier observes writer processing, not durable capture. An
    unfinished barrier may lose ordinary queued records or deliver them later;
    the result envelope and independent durable journals retain their contracts.
    A zero-record child avoids importing telemetry and OTel entirely. Deferred
    OTLP startup must still be attempted before interpreter finalization.
    """
    if "base.telemetry" not in sys.modules:
        return
    import ava
    from base import telemetry

    context = getattr(ava, "context", None)
    owned = clients if clients is not None else (None if context is None else context.clients)
    if owned is not None:
        owned_result = owned.sync_events()
        if owned_result.status is telemetry.DrainStatus.UNFINISHED:
            logger.warning(
                "exec child: owned telemetry delivery is unfinished; "
                "queued records may be lost or land later"
            )
    result = telemetry.sync()
    if result.status is telemetry.DrainStatus.UNFINISHED:
        logger.warning(
            "exec child: ordinary telemetry delivery is unfinished; "
            "queued records may be lost or land later"
        )
    if "base.telemetry.otlp.telemetry_otlp" in sys.modules:
        from base.telemetry.otlp import telemetry_otlp

        telemetry_otlp.finalize()


def _deliver_envelope_telemetry(*, clients: Any = None) -> None:
    """Best-effort last-mile delivery for the crash-envelope writers (task #4312).

    Never raises and never rewrites the envelope: the crash must stay the
    reported failure. Ordinary observation delivery can remain unfinished.
    """
    try:
        _finalize_telemetry(clients=clients)
    except BaseException:
        logger.opt(exception=True).warning(
            "exec child: telemetry delivery after a crash envelope failed; "
            "ordinary observation delivery is unfinished; queued records may be lost or land later"
        )


def _deliver_run_telemetry(result_path: str, payload: Any) -> None:
    """Attempt ordinary observation delivery after writing this run's envelope.

    Known unfinished delivery is best effort and preserves the business result.
    Unknown sync/flush failures retain the existing crash boundary for successful
    runs; an existing crash stays primary. Timed-out or cancelled children skip
    delivery because their parent is already stopping them.
    """
    if payload.kind not in ("done", "lifecycle", "crashed"):
        return
    try:
        _finalize_telemetry()
    except BaseException as exc:
        logger.opt(exception=True).warning(
            "exec child: post-run telemetry delivery failed (run outcome: {}); "
            "ordinary observation delivery is unfinished; queued records may be lost or land later",
            payload.kind,
        )
        # A post-run telemetry sync/flush failure must not read as a clean
        # outcome: report it with the REAL code_reached flag (P0 #2100). A
        # crashed envelope already carries its own failure.
        if payload.kind in ("done", "lifecycle"):
            _write_crashed_result(result_path, exc, code_reached=payload.code_reached)


def _bind_identity(request: RequestPayload, *, clients: Any) -> None:
    """Bind this child's `AvaContext` and the incarnation from its validated request."""
    import ava
    from base.agents.context import AvaContext

    ava.bind_context(
        AvaContext.from_description(
            request.context, original_incarnation=request.incarnation, clients=clients
        )
    )
    # No eager OTLP warmup: the backend comes up lazily on the first export
    # (`_ensure()` in base/telemetry/otlp/telemetry_otlp.py), so a zero-record
    # child never imports the OTel SDK at all (task #3816 M3).


def _run(
    request_path: str, result_path: str, boot_started_at: float, *, config: Any, clients: Any
) -> None:
    """Child body: read the request, set up identity + plugins + state, run the
    code, write the result envelope."""
    child = _import_runtime(boot_started_at)
    from agent.graph.exec.protocol import ResultPayload, read_request, write_result

    _line_buffered_output()
    _install_signal_handlers()
    from base.cluster.machine import validate_machine_name

    raw_agent_id = os.environ.get("AVA_AGENT_ID")
    _init_logger(
        None if raw_agent_id is None else int(raw_agent_id),
        producer=clients.event_pipeline,
        machine_reader=lambda: validate_machine_name(config.view.general.machine_name),
    )
    request = read_request(Path(request_path))
    payload = ResultPayload(kind="done")

    birth, overlay = _pop_overlay_env()
    from base.clock import Clock, clock_config_from_boot

    _bind_identity(request, clients=clients)

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config))

    # Two-phase overlay application, mirroring the agent process's own boot:
    # framework fields early (before any settings read), plugin fields after
    # plugins load (the SDK installation owns the bound plugin config image).
    framework_overlay_applied = _apply_overlay_scope(
        birth, overlay, scope="framework", set_framework_field=config.set_field
    )
    import ava

    _init_logger(
        request.agent_id,
        producer=ava.context.clients.event_pipeline,
        machine_reader=lambda: validate_machine_name(config.view.general.machine_name),
    )
    # Load plugin namespaces (ava.tasks etc.) + wraps into this process — the
    # same explicit load a watcher child runs. Idempotent, surface-only: a
    # request carrying a state snapshot arms a lazy slot whose first use
    # upgrades to the agent-runtime faces (state fields feed the state schema)
    # — the child start stays off the agent runtime either way (task #3633).
    # The install applies the env baseline AVA_SDK_DISABLE as part of the load.
    child.ava.ensure_plugins_loaded(
        config=config,
        clock_factory=clock_factory,
        producer=ava.context.clients.event_pipeline,
    )
    from dataclasses import replace

    from ava.sdk_surface import settings as sdk_settings

    ava.bind_context(
        replace(ava.context, catalog=sdk_settings.model_catalog(), clock_factory=clock_factory)
    )
    _apply_overlay_scope(birth, overlay, scope="plugin")

    def read_agent_default(_domain: str, field: str) -> Any:
        return sdk_settings.config_authority().service_field_value(field)

    if framework_overlay_applied:
        # Per-agent sdk_disable additions ride the overlay; they apply additively
        # on top of the installed surface (delta — only new entries take effect).
        from agent.process_boot import _apply_per_agent_sdk_disable

        _apply_per_agent_sdk_disable(default_reader=read_agent_default)
    # A text-only agent gets no attach contract anywhere in its SDK docs,
    # including interactive `ava.help(ava.self)` (user ruling 2026-08-28): the
    # renderer computes the child's media gating itself, per call.
    from agent.process_boot import _apply_per_agent_eval_isolation

    _apply_per_agent_eval_isolation(default_reader=read_agent_default)
    _build_state_slot(child, request)

    if request.timeout_s > 0:
        _arm_watchdog(request.timeout_s)

    try:
        _emit_child_boot_timing(child)
    except BaseException as exc:
        # Boot-timing failure: the code never ran.
        _write_crashed_result(result_path, exc, code_reached=False)
        _deliver_envelope_telemetry()
        return
    try:
        _run_code(request.code, payload)
    except BaseException as exc:
        # _run_code catches everything itself — an escape here is a framework
        # failure, not a user-code crash. Report it with the REAL code_reached
        # flag so main() never stamps it as a boot crash (P0 #2100).
        _write_crashed_result(result_path, exc, code_reached=payload.code_reached)
        _deliver_envelope_telemetry()
        return
    finally:
        _take_result_state_update(child, payload, state_injected=request.state_raw is not None)
    # Outside the try: a boot-phase exception (config fetch, request read,
    # plugin load) propagates to main(), which writes the crash envelope with
    # code_reached=False. A write failure here falls back to the best-effort
    # crash envelope carrying the REAL code_reached, and is swallowed so
    # main() does not overwrite it with the boot-crash reading (P0 #2100).
    try:
        write_result(Path(result_path), payload)
    except BaseException as write_exc:
        _write_crashed_result(result_path, write_exc, code_reached=payload.code_reached)
    # Deliver queued records after the envelope write (task #4312).
    _deliver_run_telemetry(result_path, payload)


def main() -> None:
    """Entry: run one exec and exit 0 — the result envelope carries the
    semantics, not the exit code (a non-zero exit would add nothing the
    envelope does not already say, and the parent treats a missing envelope
    as the crash path anyway)."""
    boot_started_at = time.perf_counter()
    request_path = os.environ.get("AVA_EXEC_REQUEST_FILE")
    result_path = os.environ.get("AVA_EXEC_RESULT_FILE")
    if not request_path or not result_path:
        sys.stderr.write(
            "agent.execution.child needs AVA_EXEC_REQUEST_FILE and AVA_EXEC_RESULT_FILE "
            "in the environment — spawn it via agent.graph.exec._subprocess\n"
        )
        raise SystemExit(2)
    clients: Any = None
    try:
        _line_buffered_output()
        _install_signal_handlers()
        from ava.sdk_surface.process_context import process_clients
        from base.config import ConfigBoot

        config = ConfigBoot()
        config.boot()
        clients = process_clients(config=config)
        _run(request_path, result_path, boot_started_at, config=config, clients=clients)
    except BaseException as exc:
        _write_crashed_result(result_path, exc)
        # The crash envelope's record needs the same last-mile delivery (task
        # #4312).
        _deliver_envelope_telemetry(clients=clients)
    finally:
        # The connections this child opened (SQL, Redis, gateway, MCP) end with it.
        sdk = sys.modules.get("ava")
        context = None if sdk is None else sdk.unbind_context()
        if context is not None and context.clients is not clients:
            context.clients.close()
        if clients is not None:
            clients.close()


def _write_crashed_result(
    result_path: str, exc: BaseException, *, code_reached: bool | None = False
) -> None:
    """Best-effort crash envelope that cannot itself hide the original failure.

    `code_reached` defaults to False: this is the boot-crash writer (main()'s
    handler) — the code never ran. `_run`'s envelope-write failure passes the
    payload's own value, which is True after the code reached exec."""
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
        "code_reached": code_reached,
        "state_update_error": None,
        "sdk_calls": None,
    }
    try:
        from agent.graph.exec.protocol import ResultPayload as RuntimeResultPayload
        from agent.graph.exec.protocol import write_result as runtime_write_result

        runtime_write_result(
            Path(result_path),
            RuntimeResultPayload(
                kind="crashed",
                exc_type=exc_type,
                exc_msg=exc_msg,
                full_traceback=full_traceback,
                code_reached=code_reached,
            ),
        )
        return
    except BaseException:
        # The fallback writes first: the envelope must land even if logging is what is broken.
        try:
            _write_crashed_result_stdlib(result_path, envelope)
        finally:
            logger.opt(exception=True).warning(
                "exec child: the result envelope writer failed; fell back to the stdlib writer"
            )


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
        # A closed or broken stderr (OSError / ValueError) leaves only the log line below.
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.write(
                "exec child could not write crash result envelope: "
                f"{type(write_exc).__name__}: {write_exc}\n"
            )
            sys.stderr.flush()
        logger.opt(exception=True).warning(
            "exec child: the stdlib crash envelope write failed; the parent sees no result envelope"
        )


def _format_current_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


if __name__ == "__main__":
    main()
