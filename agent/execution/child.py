# ava:ref agent/execution/docs/child-boundaries.ava.okf.md#exec-child-result-boundary

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

from agent.graph.exec.protocol import RequestPayload
from base.log import logger

WATCHDOG_MARGIN_S = 5.0


WATCHDOG_EXIT_CODE = 124

_RESULT_KIND: dict[type[BaseException], Literal["cancelled", "timed_out"]] = {
    KeyboardInterrupt: "cancelled",
    TimeoutError: "timed_out",
}


class _Sdk(Protocol):
    state: Any
    state_update: dict[str, Any] | None

    def ensure_plugins_loaded(
        self,
        *,
        surface: bool = True,
        config: Any = None,
        clock_factory: Callable[[], Any] | None = None,
        producer: Callable[[], Any] | None = None,
        validate_state: Callable[[Any], object] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class ChildContext:
    ava: _Sdk
    boot_started_at: float


def import_runtime(boot_started_at: float) -> ChildContext:
    import ava

    return ChildContext(ava=ava, boot_started_at=boot_started_at)


def line_buffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def _install_signal_handlers() -> None:

    def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def _raise_timeout_error(_signum: int, _frame: object) -> None:
        raise TimeoutError("exec subprocess timed out")

    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, _raise_timeout_error)


def _arm_watchdog(timeout_s: float) -> None:
    from base.native_process.exec_domain import KILL_GRACE_S

    margin = float(os.environ.get("AVA_EXEC_WATCHDOG_MARGIN_S", WATCHDOG_MARGIN_S))
    delay = timeout_s + KILL_GRACE_S + margin

    def _timeout() -> None:
        os._exit(WATCHDOG_EXIT_CODE)

    timer = threading.Timer(delay, _timeout)

    timer.daemon = True
    timer.start()


def pop_overlay_env() -> tuple[dict[str, object] | None, dict[str, object] | None]:
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


def apply_overlay_scope(
    birth: dict[str, object] | None,
    overlay: dict[str, object] | None,
    *,
    scope: Literal["framework", "plugin"],
    set_framework_field: Callable[[str, object], None] | None = None,
) -> bool:
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


def init_logger(
    agent_id: int | None,
    *,
    producer: Callable[[], Any],
    machine_reader: Callable[[], str],
) -> None:
    if agent_id is None:
        return
    from base.log.process import init_subprocess_logger

    init_subprocess_logger(agent_id=agent_id)
    try:
        from base.telemetry.process_logging import add_event_sink

        add_event_sink(
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
    producer().defer_exports()


def emit_child_boot_timing(child: ChildContext, *, clients: Any) -> None:
    duration_ms = (time.perf_counter() - child.boot_started_at) * 1000
    extra: dict[str, object] = {}
    deferred = clients.exports_deferred()
    if deferred is not None:
        extra["otlp_deferred"] = deferred
    logger.info(
        "exec child boot completed in {duration_ms:.1f}ms",
        event="exec_child_boot",
        duration_ms=duration_ms,
        **extra,
    )


class _LazyStateSlot:
    def __init__(self, payload: RequestPayload, ava: _Sdk) -> None:
        object.__setattr__(self, "_payload", payload)
        object.__setattr__(self, "_ava", ava)
        object.__setattr__(self, "_real", None)

    def materialize(self) -> None:
        if object.__getattribute__(self, "_real") is not None:
            return

        ava: _Sdk = object.__getattribute__(self, "_ava")
        from agent.state import build_agent_state, plugin_state_schema

        ava.ensure_plugins_loaded(surface=False, validate_state=plugin_state_schema)
        payload: RequestPayload = object.__getattribute__(self, "_payload")
        from base.packages.plugins.loading.registry import build_registry

        state_cls = build_agent_state(build_registry(validate_state=plugin_state_schema))
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


def build_state_slot(child: ChildContext, payload: RequestPayload) -> None:
    if payload.state_raw is None:
        return
    child.ava.state = _LazyStateSlot(payload, child.ava)
    child.ava.state_update = {}


def _take_result_state_update(child: ChildContext, payload: Any, *, state_injected: bool) -> None:
    if not state_injected:
        return
    update = child.ava.state_update
    if not isinstance(update, dict):
        payload.state_update_error = (
            f"plugin tampered with ava.state_update: expected dict, got {type(update).__name__}"
        )
        return

    payload.state_update = update or None


def run_code(code: str, payload: Any) -> None:
    from agent.graph.agent_traceback import (
        format_agent_traceback,
        format_full_traceback,
        register_agent_source,
    )
    from ava.sdk_surface import process_context
    from ava.sdk_surface.help import HelpRouter
    from ava.sdk_surface.installation import installed
    from base.agents.sdk import telemetry as sdk_usage_telemetry

    register_agent_source(code)
    builtins_source = (
        cast(dict[str, Any], __builtins__)
        if isinstance(__builtins__, dict)
        else cast(dict[str, Any], vars(__builtins__))
    )

    builtins_map = dict(builtins_source)
    original_help = builtins_map["help"]
    builtins_map["help"] = HelpRouter(original_help)
    fresh_globals: dict[str, Any] = {
        "__name__": "__agent_code__",
        "__builtins__": builtins_map,
    }
    tally = sdk_usage_telemetry.SdkCallTally()
    execution_context = process_context.current_context()
    installation = installed()
    sampling = (
        contextlib.nullcontext() if installation is None else installation.sampling.execution()
    )
    process_context.bind_context(replace(execution_context, sdk_calls=tally))
    try:
        payload.code_reached = True

        with sampling:
            exec(compile(code, "<agent_code>", "exec"), fresh_globals)
    except BaseException as exc:
        from base.agents.lifecycle import LifecycleExit

        if isinstance(exc, LifecycleExit):
            payload.kind = "lifecycle"
            payload.lifecycle_type = type(exc).__name__
            return

        payload.kind = _RESULT_KIND.get(type(exc), "crashed")
        payload.exc_type = type(exc).__name__
        payload.exc_msg = str(exc)[:2000]
        payload.full_traceback = format_full_traceback(exc)
        sys.stdout.write(format_agent_traceback(exc))
        sys.stdout.flush()
    finally:
        payload.sdk_calls = sdk_usage_telemetry.tally_entries(tally.snapshot())
        process_context.bind_context(execution_context)


def finalize_telemetry(*, clients: Any = None) -> None:
    if "base.telemetry" not in sys.modules:
        return
    from base import telemetry

    sdk = sys.modules.get("ava")
    context = None if sdk is None else getattr(sdk, "context", None)
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
    if owned is not None:
        owned.finalize_events()


def deliver_envelope_telemetry(*, clients: Any = None) -> None:
    try:
        finalize_telemetry(clients=clients)
    except BaseException:
        logger.opt(exception=True).warning(
            "exec child: telemetry delivery after a crash envelope failed; "
            "ordinary observation delivery is unfinished; queued records may be lost or land later"
        )


def deliver_run_telemetry(result_path: str, payload: Any) -> None:
    if payload.kind not in ("done", "lifecycle", "crashed"):
        return
    try:
        finalize_telemetry()
    except BaseException as exc:
        logger.opt(exception=True).warning(
            "exec child: post-run telemetry delivery failed (run outcome: {}); "
            "ordinary observation delivery is unfinished; queued records may be lost or land later",
            payload.kind,
        )

        if payload.kind in ("done", "lifecycle"):
            write_crashed_result(result_path, exc, code_reached=payload.code_reached)


def _bind_identity(
    request: RequestPayload, *, context: Any, clock_factory: Callable[[], Any]
) -> None:
    from ava.sdk_surface import process_context
    from base.agents.context import AvaContext

    process_context.bind_context(
        replace(
            AvaContext.from_description(
                request.context,
                original_incarnation=request.incarnation,
                clients=context.clients,
                machine=context.require_machine(),
            ),
            clock_factory=clock_factory,
        )
    )


def _run(
    request_path: str, result_path: str, boot_started_at: float, *, config: Any, context: Any
) -> None:
    child = import_runtime(boot_started_at)
    from agent.graph.exec.protocol import ResultPayload, read_request, write_result

    line_buffered_output()
    _install_signal_handlers()
    raw_agent_id = os.environ.get("AVA_AGENT_ID")
    init_logger(
        None if raw_agent_id is None else int(raw_agent_id),
        producer=context.clients.event_pipeline,
        machine_reader=context.require_machine().name,
    )
    request = read_request(Path(request_path))
    payload = ResultPayload(kind="done")

    birth, overlay = pop_overlay_env()
    from base.clock import SYSTEM_TIMERS, Clock
    from base.clock.configuration import clock_config_from_boot

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    _bind_identity(request, context=context, clock_factory=clock_factory)

    framework_overlay_applied = apply_overlay_scope(
        birth, overlay, scope="framework", set_framework_field=config.set_field
    )
    import ava

    init_logger(
        request.agent_id,
        producer=ava.context.clients.event_pipeline,
        machine_reader=context.require_machine().name,
    )
    from ava.boot import initialize_context

    initialize_context(
        ava.context,
        config=config,
        clock_factory=clock_factory,
        producer=ava.context.clients.event_pipeline,
    )
    from ava.sdk_surface import settings as sdk_settings

    apply_overlay_scope(birth, overlay, scope="plugin")

    def read_agent_default(_domain: str, field: str) -> Any:
        return sdk_settings.config_authority().service_field_value(field)

    if framework_overlay_applied:
        from agent.process_boot import apply_per_agent_sdk_disable

        apply_per_agent_sdk_disable(default_reader=read_agent_default)

    from agent.process_boot import apply_per_agent_eval_isolation

    apply_per_agent_eval_isolation(default_reader=read_agent_default)
    build_state_slot(child, request)

    if request.timeout_s > 0:
        _arm_watchdog(request.timeout_s)

    try:
        emit_child_boot_timing(child, clients=context.clients)
    except BaseException as exc:
        write_crashed_result(result_path, exc, code_reached=False)
        deliver_envelope_telemetry()
        return
    try:
        run_code(request.code, payload)
    except BaseException as exc:
        write_crashed_result(result_path, exc, code_reached=payload.code_reached)
        deliver_envelope_telemetry()
        return
    finally:
        _take_result_state_update(child, payload, state_injected=request.state_raw is not None)

    try:
        write_result(Path(result_path), payload)
    except BaseException as write_exc:
        write_crashed_result(result_path, write_exc, code_reached=payload.code_reached)

    deliver_run_telemetry(result_path, payload)


def main() -> None:
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
        line_buffered_output()
        _install_signal_handlers()
        from ava.boot import process_clients
        from base.agents.context import AvaContext
        from base.cluster.machine.boot import machine_from_boot
        from base.config import ConfigBoot

        config = ConfigBoot()
        config.boot()
        machine = machine_from_boot(config)
        clients = process_clients(config=config, machine=machine)
        context = AvaContext(clients=clients, machine=machine)
        _run(request_path, result_path, boot_started_at, config=config, context=context)
    except BaseException as exc:
        write_crashed_result(result_path, exc)

        deliver_envelope_telemetry(clients=clients)
    finally:
        slot = sys.modules.get("ava.sdk_surface.process_context")
        context = None if slot is None else slot.unbind_context()
        if context is not None and context.clients is not clients:
            context.clients.close()
        if clients is not None:
            clients.close()


def write_crashed_result(
    result_path: str, exc: BaseException, *, code_reached: bool | None = False
) -> None:
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
        try:
            _write_crashed_result_stdlib(result_path, envelope)
        finally:
            logger.opt(exception=True).warning(
                "exec child: the result envelope writer failed; fell back to the stdlib writer"
            )


def _write_crashed_result_stdlib(result_path: str, envelope: dict[str, object]) -> None:
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


def run_as_entry() -> None:
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()


if __name__ == "__main__":
    run_as_entry()
