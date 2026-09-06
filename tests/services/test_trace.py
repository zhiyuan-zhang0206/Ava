"""Tests for shared.trace — local OTLP-JSON span recording (no network)."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from shared import telemetry_otlp
from shared import trace as trace_mod
from shared.trace import (
    OtlpJsonHttpSpanExporter,
    claim_idle_wait_span,
    initialize_tracing,
    turn_span,
)


@pytest.fixture(autouse=True)
def _production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True, raising=False)


_TRACE_BG_THREAD_NAMES = frozenset({"trace-arm", "trace-collector-retry"})


def _assert_no_background_trace_threads() -> None:
    """A background trace thread from a previous test must not be in flight —
    a zombie arm finishing inside the next test runs whatever Traceloop.init
    that test has installed (the #1065 delta attempt 1 flake: a second call
    counted in test_idempotent_second_call_is_noop's lambda)."""
    leftovers = [
        t.name for t in threading.enumerate() if t.name in _TRACE_BG_THREAD_NAMES and t.is_alive()
    ]
    assert not leftovers, f"previous test leaked background thread(s): {leftovers}"


_THREAD_DRAIN_TIMEOUT_S = 5.0


def _drain_background_trace_threads() -> None:
    """Join the module's background threads to death BEFORE the next test runs.

    The arm thread runs the ~3s traceloop import + Traceloop.init on a daemon
    thread; a test body that returns while it is still in flight used to leave
    a zombie that completed inside the NEXT test and called whatever
    Traceloop.init that test had installed — counted as a second init call
    (the #1065 delta attempt 1 flake). The arm sets init_resolved in its
    finally, so wait on that event first (deterministic - the event is set
    when the arm actually finishes), then join the thread itself. Bounded:
    a hung arm is caught by the next test's _assert_no_background_trace_threads
    with a loud failure instead of a silent cross-test contamination.
    """
    retry_thread = trace_mod._state["retry_thread"]
    if isinstance(retry_thread, threading.Thread):
        retry_thread.join(timeout=_THREAD_DRAIN_TIMEOUT_S)
    arm_thread = trace_mod._state["arm_thread"]
    if isinstance(arm_thread, threading.Thread) and arm_thread.is_alive():
        init_resolved = trace_mod._state["init_resolved"]
        if isinstance(init_resolved, threading.Event):
            init_resolved.wait(timeout=_THREAD_DRAIN_TIMEOUT_S)
        arm_thread.join(timeout=_THREAD_DRAIN_TIMEOUT_S)
        assert not arm_thread.is_alive(), (
            "trace arm thread still alive after the teardown drain — "
            "a slow import/init escaped the bounds and leaked into the next "
            "test (the guard in _arm_tracing keeps the survivor inert, but the "
            "leak itself must be diagnosed here, not in a later assertion)"
        )


@pytest.fixture(autouse=True)
def _reset_init_flag():
    """Reset trace-init and retry-loop state between tests."""
    _assert_no_background_trace_threads()
    gate = getattr(telemetry_otlp, "_observability_export_allowed", None)
    if gate is not None:
        gate.cache_clear()
    trace_mod._state.clear()
    trace_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
        arm_thread=None,
        init_resolved=threading.Event(),
        arm_failed=False,
        timeout_reported=False,
    )
    if gate is not None:
        gate.cache_clear()
    yield
    trace_mod._state["initialized"] = True
    _drain_background_trace_threads()
    trace_mod._state.clear()
    trace_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
        arm_thread=None,
        init_resolved=threading.Event(),
        arm_failed=False,
        timeout_reported=False,
    )


def _wait_init_resolved(timeout: float = 5.0) -> None:
    """Join the background arming pass: the armed-path tests assert on state
    the arm thread writes, so they must wait for it (deterministic, not a
    sleep — the event is set in the thread's finally)."""
    resolved = trace_mod._state.get("init_resolved")
    assert isinstance(resolved, threading.Event)
    assert resolved.wait(timeout=timeout), "background trace arming did not resolve"


@pytest.fixture(autouse=True)
def _collector_up(monkeypatch: pytest.MonkeyPatch):
    """The local-collector preflight must pass by default — the tests exercise
    the exporter/init logic, not the network probe (which is covered by its own
    telemetry_otlp tests)."""
    monkeypatch.setattr(
        "shared.trace.endpoint_reachable",
        lambda _e: True,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_disabled_returns_early(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, initialize_tracing returns None and does not init the SDK."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)
    assert initialize_tracing() is None
    assert trace_mod._state["initialized"] is False


def _under_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk usage below the watermark: initialize_tracing's auto-degrade guard
    must not skip recording in tests (the dev disk can be >90% full and would
    otherwise make every init-path test environment-dependent)."""
    monkeypatch.setattr("shared.trace._disk_usage", lambda: (0.1, 100 * 1024**3))


def test_enabled_inits_traceloop_with_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """trace_enabled=True: Traceloop.init is handed an OtlpJsonHttpSpanExporter
    pointed at the LOCAL collector as the sole exporter (no api_endpoint/api_key
    network sink), batch on, traceloop's own telemetry off, plus the instruments
    set covering Anthropic/OpenAI/LangChain/Google."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    monkeypatch.setattr(trace_mod, "cluster_label", lambda: ".ava-test")
    monkeypatch.setattr("shared.observability.production_identity", lambda: False)
    _under_watermark(monkeypatch)

    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing()
    _wait_init_resolved()

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    kw = calls[0]
    exporter = kw["exporter"]
    assert isinstance(exporter, OtlpJsonHttpSpanExporter)
    assert exporter._endpoint == "http://127.0.0.1:4318/v1/traces"
    assert "api_endpoint" not in kw  # no SDK-level network sink; the exporter owns the POST
    assert "api_key" not in kw
    assert kw["telemetry_enabled"] is False
    assert kw["disable_batch"] is False
    assert kw["resource_attributes"] == {
        "cluster": ".ava-test",
        "service.line": "ava",
        "environment": "dev",
    }

    from traceloop.sdk.instruments import Instruments

    instruments = kw["instruments"]
    assert Instruments.ANTHROPIC in instruments
    assert Instruments.OPENAI in instruments
    assert Instruments.LANGCHAIN in instruments
    assert Instruments.GOOGLE_GENERATIVEAI in instruments

    assert trace_mod._state["initialized"] is True


def test_gateway_trace_recording_skips_without_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True)
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp._observability_export_allowed.cache_clear()
    calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", record_init)

    initialize_tracing()

    assert calls == []
    assert trace_mod._state["initialized"] is False


def test_gateway_trace_recording_arms_with_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "lgtm-host").touch()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True)
    monkeypatch.setattr("shared.observability.production_identity", lambda: True)
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path / "traces")
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp._observability_export_allowed.cache_clear()
    _under_watermark(monkeypatch)
    calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", record_init)

    initialize_tracing()
    _wait_init_resolved()

    assert len(calls) == 1
    assert calls[0]["resource_attributes"] == {
        "cluster": ".ava",
        "service.line": "ava",
        "environment": "prod",
    }


def test_sdk_initialize_failure_logs_and_state_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """If Traceloop.init() itself raises on the arm thread, the failure is
    logged (not propagated — tracing is observability, not a boot blocker),
    _initialized stays False, and the wait resolves so turns still run."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    def _raise(**_kw):
        raise RuntimeError("boom: init failed")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _raise)  # pyright: ignore[reportUnknownArgumentType]
    warnings: list[tuple] = []
    monkeypatch.setattr(
        "shared.trace.logger.warning",
        lambda *a, **kw: warnings.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    initialize_tracing()  # must not raise: the failure is the arm thread's
    _wait_init_resolved()

    assert trace_mod._state["initialized"] is False
    assert trace_mod._state["arm_failed"] is True
    assert warnings
    attrs = warnings[0][1]
    assert attrs.get("action") == "recording_init_failed"  # pyright: ignore[reportUnknownMemberType]


def test_arm_failure_blocks_rearming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One arm attempt per process: after a failed init, a later call (e.g. a
    collector-retry re-entry) must NOT run Traceloop.init again — the
    TracerWrapper singleton would fake-succeed without the instrumentors."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    calls: list[int] = []

    def _fail_then_record(**_kw: object) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _fail_then_record)

    initialize_tracing()
    _wait_init_resolved()
    assert trace_mod._state["arm_failed"] is True

    initialize_tracing()  # must be a no-op, not a second arming attempt
    assert calls == [1]
    assert trace_mod._state["initialized"] is False


def test_idempotent_second_call_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Second call within the same process does not re-initialize."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing()
    initialize_tracing()
    _wait_init_resolved()

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_collector_unreachable_retries_once_until_init_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One daemon loop retries a collector-unreachable preflight, logs the
    episode once, and exits after tracing initializes."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.trace.COLLECTOR_RETRY_INTERVAL_S", 0.1)
    _under_watermark(monkeypatch)

    reachable = iter((False, False, True))
    attempts: list[bool] = []

    def endpoint_reachable(_endpoint: str) -> bool:
        result = next(reachable)
        attempts.append(result)
        return result

    monkeypatch.setattr("shared.trace.endpoint_reachable", endpoint_reachable)
    warnings: list[str] = []

    def capture_warning(message: str, *_args: object, **_kwargs: object) -> None:
        warnings.append(message)

    monkeypatch.setattr(trace_mod.logger, "warning", capture_warning)
    initialized = threading.Event()

    def init_traceloop(**_kwargs: object) -> None:
        initialized.set()

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", init_traceloop)

    initialize_tracing()
    retry_thread = trace_mod._state["retry_thread"]
    assert isinstance(retry_thread, threading.Thread)

    initialize_tracing()
    assert trace_mod._state["retry_thread"] is retry_thread
    assert initialized.wait(timeout=1.0)
    retry_thread.join(timeout=0.5)
    _wait_init_resolved()

    assert attempts == [False, False, True]
    assert warnings == ["trace recording disabled — local OTel collector not answering"]
    assert trace_mod._state["initialized"] is True
    assert trace_mod._state["collector_offline_reported"] is False
    assert not retry_thread.is_alive()


def test_arming_runs_off_the_caller_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The heavy part never blocks the boot path: initialize_tracing returns
    while Traceloop.init is still pending (the mock blocks until released),
    and ensure_init_resolved() is what the use sites wait on."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        assert release.wait(timeout=5.0), "test released the arm thread"

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing()  # returns immediately — the mock is still blocked
    assert entered.wait(timeout=5.0), "arm thread must have started"
    assert trace_mod._state["initialized"] is False  # boot already returned; init pending

    release.set()
    _wait_init_resolved()
    assert trace_mod._state["initialized"] is True


def test_teardown_drains_pending_arm_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A test body that returns while the arm thread is still in flight must
    not leave a zombie behind: its delayed Traceloop.init would then land in
    the NEXT test's monkeypatched lambda (counted as a second init — the
    #1065 delta attempt 1 flake). This test only has to leave one in flight;
    the _reset_init_flag teardown has to drain it, and the setup boundary
    check turns a leftover into a deterministic failure."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        release.wait(timeout=1.0)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing()
    arm_thread = trace_mod._state["arm_thread"]
    assert isinstance(arm_thread, threading.Thread)
    assert arm_thread.is_alive()
    # Intentionally no _wait_init_resolved(): the fixture teardown must drain.


def test_arm_tracing_skips_init_when_already_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Traceloop.init runs at most once per process: _arm_tracing is a no-op
    when a previous arm already succeeded (initialized) or failed
    (arm_failed). A second arm thread is only reachable when test state was
    reset while the first was in flight, but without this guard its init
    would fake-succeed (the SDK's TracerWrapper singleton keeps the first
    init's instrumentor set and reports success) AND land in whichever test
    installed the current monkeypatch — the #1065 / post-#1068 2-call flake.
    """
    from shared.trace import _arm_tracing

    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    trace_mod._state["initialized"] = True
    trace_mod._state["arm_failed"] = False
    _arm_tracing("http://127.0.0.1:4318")
    assert calls == []

    trace_mod._state["initialized"] = False
    trace_mod._state["arm_failed"] = True
    _arm_tracing("http://127.0.0.1:4318")
    assert calls == []


def test_concurrent_arm_threads_init_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Two arm threads racing — a zombie escaped a test's teardown plus the
    next test's own arm, both completing inside one test — run
    Traceloop.init exactly once: the second thread reads the first's outcome
    (under the init lock, after the import) before calling init. Without the
    guard the SDK's TracerWrapper singleton would let both init calls run
    (the second fake-succeeds), which is the #1065 / post-#1068 2-call flake.
    """
    from shared.trace import _arm_tracing

    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    entered = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def _slow_init(**_kw: object) -> None:
        entered.set()
        assert release.wait(timeout=5.0), "test released the first arm's init"
        calls.append(_kw)  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    first = threading.Thread(target=_arm_tracing, args=("http://127.0.0.1:4318",), daemon=True)
    first.start()
    assert entered.wait(timeout=5.0), "first arm thread must reach Traceloop.init"

    # State reset while the first arm is in flight — exactly what the fixture
    # teardown does when a slow CI import outlives the drain.
    trace_mod._state.clear()
    trace_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
        arm_thread=None,
        init_resolved=threading.Event(),
        arm_failed=False,
        timeout_reported=False,
    )

    second = threading.Thread(target=_arm_tracing, args=("http://127.0.0.1:4318",), daemon=True)
    second.start()
    release.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_turn_span_waits_for_pending_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first-turn contract: turn_span blocks while the arm thread is
    pending and opens the root span only after the arm resolves — a span
    opened against the unset proxy tracer would be silently lost."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = False
    release = threading.Event()
    arm = threading.Thread(target=release.wait, daemon=True, name="fake-arm")
    trace_mod._state["arm_thread"] = arm
    arm.start()

    entered: list[str] = []
    before_wait = threading.Event()

    def _enter_span() -> None:
        before_wait.set()
        with turn_span(name="t", session_id="s", turn=1):
            entered.append("open")

    t = threading.Thread(target=_enter_span)
    t.start()
    assert before_wait.wait(timeout=5)
    assert entered == []  # blocked in ensure_init_resolved, span NOT opened yet

    trace_mod._state["initialized"] = True
    trace_mod._state["init_resolved"].set()
    t.join(timeout=5)
    assert entered == ["open"]  # opened only after the arm resolved
    assert not t.is_alive()
    release.set()
    arm.join(timeout=5)


def test_ensure_init_resolved_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung arm must not hang the first turn forever: the wait is bounded,
    logs once, and later calls skip the wait entirely."""
    from shared.trace import ensure_init_resolved

    release = threading.Event()
    arm = threading.Thread(target=release.wait, daemon=True, name="fake-arm")
    trace_mod._state["arm_thread"] = arm
    arm.start()
    monkeypatch.setattr(trace_mod, "_INIT_RESOLVED_TIMEOUT_S", 0.05)
    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_warning(*a: object, **kw: object) -> None:
        warnings.append((a, kw))

    monkeypatch.setattr("shared.trace.logger.warning", _capture_warning)

    ensure_init_resolved()  # times out -> one warning
    ensure_init_resolved()  # remembered -> instant return, no second warning

    assert len(warnings) == 1
    assert warnings[0][1]["action"] == "init_resolved_timeout"
    assert trace_mod._state["timeout_reported"] is True
    release.set()
    arm.join(timeout=5)


def test_arm_thread_base_exception_marks_attempt_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one-attempt contract holds for ANY arm-thread death: a
    BaseException escape (SystemExit/GeneratorExit from inside the SDK — not
    expected, but not impossible) must still set arm_failed, so a later
    initialize_tracing() cannot re-spawn the arm thread past the dead
    is_alive() guard (the QA #1060 corner: with `except Exception` only, a
    dead arm thread carrying no flag left one-attempt-per-process bypassable).
    """
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[int] = []

    def _exit_sdk(**_kw: object) -> None:
        calls.append(1)
        raise SystemExit("sdk guard exit")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _exit_sdk)

    initialize_tracing()
    _wait_init_resolved()

    assert trace_mod._state["arm_failed"] is True
    assert trace_mod._state["initialized"] is False

    initialize_tracing()  # must be a no-op: the attempt was spent
    assert calls == [1]


def test_timeout_then_late_arm_recording_comes_up_midlife(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Timeout -> late-completion contract: the bounded wait gives up and the
    first turn proceeds WITHOUT recording, but a slow (not hung) arm that
    finishes afterwards still brings recording up mid-life — later turns get
    spans; only the first turn's spans are lost (the documented price).
    Also: a later ensure call skips the wait (already remembered), so the
    timeout does not block the late-armed turn from opening its span."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.setattr(trace_mod, "_INIT_RESOLVED_TIMEOUT_S", 0.05)

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        release.wait(timeout=10.0)

    from shared.trace import ensure_init_resolved

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)
    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_warning(*a: object, **kw: object) -> None:
        warnings.append((a, kw))

    monkeypatch.setattr("shared.trace.logger.warning", _capture_warning)

    initialize_tracing()
    assert entered.wait(timeout=5.0), "arm thread must reach Traceloop.init"

    # The arm is still in flight: the bounded wait gives up (one warning),
    # recording stays off for now...
    ensure_init_resolved()
    assert len(warnings) == 1
    assert warnings[0][1]["action"] == "init_resolved_timeout"
    assert trace_mod._state["timeout_reported"] is True
    assert trace_mod._state["initialized"] is False

    # ...the arm completes late: recording comes up mid-life...
    release.set()
    _wait_init_resolved()
    assert trace_mod._state["initialized"] is True

    # ...and later calls skip the wait entirely, so the span opens for real.
    ensure_init_resolved()
    assert len(warnings) == 1
    entered_spans: list[str] = []
    with turn_span(name="t", session_id="s", turn=1):
        entered_spans.append("open")
    assert entered_spans == ["open"]


def test_ensure_init_resolved_instant_without_arming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No wait when tracing was declined or never requested — the event is
    only consulted after an arm thread was actually spawned."""
    from shared.trace import ensure_init_resolved

    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.setattr(
        "shared.trace.endpoint_reachable",
        lambda _e: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    # Skip the daemon collector retry loop: this test asserts the instant
    # no-wait contract, not the loop (that is the retry-loop test above).
    monkeypatch.setattr("shared.trace._start_collector_retry", lambda: None)

    initialize_tracing()  # declined: collector unreachable, no arm thread
    ensure_init_resolved()  # must return at once, not hang on an unset event

    assert trace_mod._state["arm_thread"] is None
    assert trace_mod._state["initialized"] is False


# --- OtlpJsonHttpSpanExporter ---------------------------------------------------


def test_otlp_exporter_posts_protobuf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each export() batch becomes one OTLP ExportTraceServiceRequest POSTed
    to <endpoint>/v1/traces with Content-Type application/x-protobuf (the wire
    format the collector's OTLP receiver accepts — its JSON receiver rejects
    the SDK's padded-base64 ids); the body parses back to the same spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    posts: list[tuple[str, bytes, dict]] = []

    class _Resp:
        def raise_for_status(self):
            pass

    def _post(url, *, content, headers, timeout):
        posts.append((url, content, headers))  # pyright: ignore[reportUnknownMemberType]
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType]

    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("ava-agent-7") as root:
        root.set_attribute("session.id", "7")
        with tracer.start_as_current_span("child"):
            pass
    provider.shutdown()

    assert len(posts) >= 1  # pyright: ignore[reportUnknownArgumentType]  # at least one export batch
    url, _body, headers = posts[0]
    assert url == "http://127.0.0.1:4318/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"

    # The body is the OTLP ExportTraceServiceRequest protobuf; it must parse
    # back to exactly the recorded spans (what Tempo ingests).
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = 0
    for _, raw, _h in posts:
        req = ExportTraceServiceRequest()
        req.ParseFromString(raw)
        assert req.SerializeToString()
        spans += sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)
    assert spans == 2


def test_otlp_exporter_timeout_is_bounded_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collector timeout costs one bounded POST and never escapes the exporter."""
    timeouts: list[float] = []

    def _timeout(_url: str, *, content: bytes, headers: dict[str, str], timeout: float):
        del content, headers
        timeouts.append(timeout)
        raise httpx.ReadTimeout("collector stalled")

    monkeypatch.setattr("httpx.post", _timeout)
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")

    assert exporter.export([]) is trace_mod.SpanExportResult.FAILURE
    assert timeouts == [trace_mod._TRACE_EXPORT_TIMEOUT_S]
    assert 0 < trace_mod._TRACE_EXPORT_TIMEOUT_S <= 5.0


def test_otlp_exporter_circuit_drops_during_cooldown_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive failures open the circuit; one post-cooldown probe closes it."""
    clock = {"now": 100.0}
    posts = 0

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    def _post(_url: str, *, content: bytes, headers: dict[str, str], timeout: float):
        nonlocal posts
        del content, headers, timeout
        posts += 1
        if posts <= trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD:
            raise httpx.ConnectError("collector unavailable")
        return _Resp()

    monkeypatch.setattr(trace_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr("httpx.post", _post)
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")

    for _ in range(trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD):
        assert exporter.export([]) is trace_mod.SpanExportResult.FAILURE
    assert posts == trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD

    assert exporter.export([]) is trace_mod.SpanExportResult.FAILURE
    assert posts == trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD
    assert exporter._dropped_batches == 1

    clock["now"] += trace_mod._TRACE_EXPORT_COOLDOWN_S
    assert exporter.export([]) is trace_mod.SpanExportResult.SUCCESS
    assert posts == trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD + 1
    assert exporter._consecutive_failures == 0

    assert exporter.export([]) is trace_mod.SpanExportResult.SUCCESS
    assert posts == trace_mod._TRACE_EXPORT_FAILURE_THRESHOLD + 2


# --- turn_span placeholder-root export timing (#1964) ------------------------------


def test_turn_span_exports_root_at_start_not_at_end(monkeypatch: pytest.MonkeyPatch):
    """The turn root is a PLACEHOLDER: ended (and exported) at turn START, so a
    trace always has its root even when the process dies mid-turn.

    Two export-timing assertions:
    1. while the turn is still running (inside `turn_span`), the exporter has
       ALREADY received the root span (carrying session.id + ava.turn);
    2. exiting the turn does NOT export the root again — the span is ended
       once, at turn start (use_span(end_on_exit=False) detaches the context
       without a second end).

    Plus the structural contract: a child span created inside the turn parents
    under the already-ended root (same trace_id, parent_span_id == root id).
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    posts: list[tuple[str, bytes, dict]] = []

    class _Resp:
        def raise_for_status(self):
            pass

    def _post(url, *, content, headers, timeout):
        posts.append((url, content, headers))  # pyright: ignore[reportUnknownMemberType]
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType]
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = otel_trace.get_tracer_provider()
    otel_trace.set_tracer_provider(provider)
    try:
        from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

        def _received_spans() -> list[OtlpSpan]:
            out: list[OtlpSpan] = []
            for _url, raw, _h in posts:
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                    ExportTraceServiceRequest,
                )

                req = ExportTraceServiceRequest()
                req.ParseFromString(raw)
                for rs in req.resource_spans:
                    for ss in rs.scope_spans:
                        out.extend(ss.spans)
            return out

        with turn_span(name="ava-agent-7", session_id="7", turn=3):
            # Assertion 1: the root is exported BEFORE the turn ends.
            spans = _received_spans()
            assert len(spans) == 1, f"root must export at turn start, got {len(spans)}"
            root = spans[0]
            assert root.name == "ava-agent-7"
            attrs = {kv.key: kv.value.string_value or kv.value.int_value for kv in root.attributes}
            assert attrs["session.id"] == "7"
            assert attrs["ava.turn"] == 3
            assert root.end_time_unix_nano > 0, "placeholder root must be ended when exported"
            root_id = root.span_id
            root_trace = root.trace_id

            # A child created inside the turn must parent under the ended root.
            tracer = otel_trace.get_tracer("ava.session")
            with tracer.start_as_current_span("child"):
                pass

        # Assertion 2: exiting the turn does not re-export the root (and the
        # child arrived, parented under the root).
        spans = _received_spans()
        roots = [s for s in spans if not s.parent_span_id]
        assert len(roots) == 1, f"root must be exported exactly once, got {len(roots)}"
        assert roots[0].span_id == root_id
        children = [s for s in spans if s.parent_span_id]
        assert len(children) == 1
        assert children[0].parent_span_id == root_id
        assert children[0].trace_id == root_trace
    finally:
        otel_trace.set_tracer_provider(previous)


# --- retention prune ---------------------------------------------------------


def test_prune_old_mirror_removes_stale_keeps_recent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_prune_old_mirror deletes files older than retention_days — legacy
    `spans-YYYYMMDD-<pid>.jsonl` AND the collector's rotated
    `spans-<ISO>.jsonl` — keeps recent ones, and never touches the unstamped
    ACTIVE `spans.jsonl` or non-mirror files."""
    from shared.trace import _prune_old_mirror

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    old = tmp_path / "spans-20200101-1.jsonl"  # well before any cutoff
    old_rotated = tmp_path / "spans-2020-01-01T00-00-00.000.jsonl"  # same, rotated name
    recent = tmp_path / "spans-20990101-1.jsonl"  # well after any cutoff
    active = tmp_path / "spans.jsonl"  # the collector's active file: never pruned
    other = tmp_path / ".ship-watermark.json"  # not a mirror file
    for p in (old, old_rotated, recent, active, other):
        p.write_text("{}\n", encoding="utf-8")

    _prune_old_mirror(retention_days=14)

    assert not old.exists()
    assert not old_rotated.exists()
    assert recent.exists()
    assert active.exists()
    assert other.exists()


def test_mirror_day_parses_all_collector_name_shapes(tmp_path: Path) -> None:
    """`_mirror_day` parses every mirror filename shape in the wild: legacy
    pid-dated files, collector-rotated backups WITH the timberjack trigger
    suffix (`-size` / `-time`), older unsuffixed rotated backups, manual
    `spans.cut-*` orphans, and the `.gz` variants of each — while the
    unstamped ACTIVE `spans.jsonl` stays None (never a prune target)."""
    from datetime import date

    from shared.trace import _mirror_day

    cases = {
        "spans-20200101-1.jsonl": date(2020, 1, 1),
        "spans-20200101-1.jsonl.gz": date(2020, 1, 1),
        "spans-2026-08-13T23-28-01.123.jsonl": date(2026, 8, 13),
        "spans-2026-08-27T03-29-10.942-size.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-time.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-size.jsonl.gz": date(2026, 8, 27),
        "spans.cut-20260827.jsonl": date(2026, 8, 27),
        "spans.cut-20260827.jsonl.gz": date(2026, 8, 27),
        "spans.jsonl": None,
        "spans.jsonl.gz": None,
        ".ship-watermark.json": None,
    }
    for name, expected in cases.items():
        p = tmp_path / name
        p.touch()
        assert _mirror_day(p) == expected, name


def test_mirror_sort_key_orders_suffixed_rotated_and_cut_files(
    tmp_path: Path,
) -> None:
    """The cap-prune order key handles timberjack-suffixed backups and manual
    cuts (day from the name, sub-day epoch from the timestamp), so a cap prune
    deletes them oldest-first instead of treating them like the active file."""
    from shared.trace import _mirror_sort_key

    names = [
        "spans-2026-08-01T00-00-00.000-size.jsonl",
        "spans-2026-08-01T01-00-00.000-time.jsonl",
        "spans.cut-20260802.jsonl",
        "spans.jsonl",
    ]
    keys = [_mirror_sort_key(tmp_path / n) for n in names]
    # Both 08-01 segments before the 08-02 cut, all before the active file.
    assert keys == sorted(keys)


def test_prune_old_mirror_removes_stale_suffixed_and_gz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_prune_old_mirror deletes stale files regardless of the rotation
    naming era — timberjack-suffixed (`-size`), manual cuts, and gzipped
    segments — and keeps the ACTIVE `spans.jsonl` untouched."""
    from shared.trace import _prune_old_mirror

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    old_suffixed = tmp_path / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old_gz = tmp_path / "spans-2020-01-01T01-00-00.000-time.jsonl.gz"
    old_cut = tmp_path / "spans.cut-20200101.jsonl"
    recent = tmp_path / "spans-2099-01-01T00-00-00.000-size.jsonl"
    active = tmp_path / "spans.jsonl"
    for p in (old_suffixed, old_gz, old_cut, recent, active):
        p.write_text("{}\n", encoding="utf-8")

    _prune_old_mirror(retention_days=14)

    assert not old_suffixed.exists()
    assert not old_gz.exists()
    assert not old_cut.exists()
    assert recent.exists()
    assert active.exists()


def test_enforce_dir_cap_counts_suffixed_and_gz_keeps_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The cap prune counts suffixed rotated and gzipped files toward the
    directory size and deletes oldest-first, and never deletes the ACTIVE
    `spans.jsonl` even when every other file carries an unrecognized-era
    name (the pre-fix bug: suffixed backups sorted with the active file and
    could be deleted in either order)."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    oldest = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    newest = tmp_path / "spans-2026-01-02T00-00-00.000-time.jsonl.gz"
    active = tmp_path / "spans.jsonl"
    for p in (oldest, newest, active):
        p.write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl*"))
    assert remaining == ["spans-2026-01-02T00-00-00.000-time.jsonl.gz", "spans.jsonl"]


def test_gzip_old_mirror_compresses_rotated_keeps_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_gzip_old_mirror compresses every non-active mirror file (rotated,
    suffixed, cut, legacy) to `.jsonl.gz` with lossless content, leaves the
    ACTIVE `spans.jsonl` alone, and is idempotent on re-run."""
    import gzip as gz

    from shared.trace import _gzip_old_mirror

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    cut = tmp_path / "spans.cut-20260827.jsonl"
    active = tmp_path / "spans.jsonl"
    rotated.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    cut.write_text('{"b":1}\n', encoding="utf-8")
    active.write_text('{"c":1}\n', encoding="utf-8")

    assert _gzip_old_mirror(grace_seconds=-1) == 2
    assert rotated.exists() is False
    assert cut.exists() is False
    gz_rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz"
    gz_cut = tmp_path / "spans.cut-20260827.jsonl.gz"
    assert gz_rotated.exists()
    assert gz_cut.exists()
    assert active.exists()  # never compressed
    assert not (tmp_path / "spans.jsonl.gz").exists()
    with gz.open(gz_rotated, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"a":1}\n{"a":2}\n'
    with gz.open(gz_cut, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"b":1}\n'
    # Idempotent: already-compressed files are skipped.
    assert _gzip_old_mirror(grace_seconds=-1) == 0


def test_gzip_old_mirror_skips_recently_written_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A file written within the grace window is skipped (a freshly cut
    active file may still be appended by the collector until its next size
    rotation); the next pass with no grace compresses it."""
    import time

    from shared.trace import _gzip_old_mirror

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    fresh = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    fresh.write_text("x\n", encoding="utf-8")
    # Touch mtime to "now" (write_text already did; keep explicit for clarity).
    now = time.time()
    import os

    os.utime(fresh, (now, now))

    assert _gzip_old_mirror(grace_seconds=3600) == 0
    assert fresh.exists()
    assert not (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz").exists()

    assert _gzip_old_mirror(grace_seconds=-1) == 1
    assert not fresh.exists()


def test_prune_old_mirror_disabled_when_nonpositive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """retention_days <= 0 disables pruning entirely."""
    from shared.trace import _prune_old_mirror

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    old = tmp_path / "spans-20200101-1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    _prune_old_mirror(retention_days=0)
    assert old.exists()


# --- turn_span -----------------------------------------------------------


class _FakeSpan:
    def __init__(self):
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self, span: _FakeSpan):
        self._span = span
        self.opened: list[str] = []

    def start_span(self, name: str):
        """turn_span uses start_span + use_span(end_on_exit=False) since the
        placeholder-root change (#1964)."""
        self.opened.append(name)
        return self._span

    @contextmanager
    def start_as_current_span(self, name: str):
        self.opened.append(name)
        yield self._span


def test_turn_span_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, turn_span is a pass-through — does not open a span."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)

    def _explode(*_a, **_kw):
        raise AssertionError("get_tracer should not be called when disabled")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_noop_when_initialize_skipped(monkeypatch: pytest.MonkeyPatch):
    """Even with trace_enabled=True, if initialize_tracing hasn't run yet,
    turn_span stays no-op — otherwise it opens a span against an
    uninitialized provider."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    assert trace_mod._state["initialized"] is False

    def _explode(*_a, **_kw):
        raise AssertionError("must not open a span when uninitialized")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_opens_root_with_session_id(monkeypatch: pytest.MonkeyPatch):
    """When enabled and initialized, turn_span opens an OTel root span with
    the given name and stamps the session and turn attributes."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    span = _FakeSpan()
    tracer = _FakeTracer(span)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="ava-agent-42", session_id="42", turn=3):
        # The placeholder root is ended (exported) at turn START, while the
        # turn is still running (#1964) — the trace has its root even when
        # the process dies mid-turn.
        assert span.ended is True

    assert tracer.opened == ["ava-agent-42"]
    assert span.attributes == {
        "session.id": "42",
        "ava.turn": 3,
    }


# --- claim idle-wait span -------------------------------------------------


class _NodeFakeSpan:
    """Fake of a LangChain node span (the SDK surface claim_idle_wait_span
    touches: name, is_recording, end)."""

    def __init__(self, name: str, recording: bool = True):
        self.name = name
        self._recording = recording
        self.ended = False

    def is_recording(self) -> bool:
        return self._recording

    def end(self) -> None:
        self.ended = True


def test_claim_idle_wait_span_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """trace_enabled=False: pass-through — no OTel call at all."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)

    def _explode(*_a, **_kw):
        raise AssertionError("OTel must not be touched when tracing is disabled")

    monkeypatch.setattr("opentelemetry.trace.get_current_span", _explode)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass


def test_claim_idle_wait_span_noop_when_initialize_skipped(
    monkeypatch: pytest.MonkeyPatch,
):
    """trace_enabled=True but initialize_tracing never ran: no-op, same as
    turn_span — a span opened against the unset proxy tracer is silently lost."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    assert trace_mod._state["initialized"] is False

    def _explode(*_a, **_kw):
        raise AssertionError("must not open a span when uninitialized")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("opentelemetry.trace.get_current_span", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass


def test_claim_idle_wait_span_ends_node_span_and_opens_idle_wait(
    monkeypatch: pytest.MonkeyPatch,
):
    """Enabled + initialized + a recording `execute_task claim` span current:
    the node span is ended at the park boundary (so the claim span shows only
    the real dispatch) and an explicit `claim idle-wait` span is opened for
    the wait."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    node_span = _NodeFakeSpan("execute_task claim")
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: node_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert node_span.ended is True
    assert tracer.opened == ["claim idle-wait"]


def test_claim_idle_wait_span_never_ends_non_node_span(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fail-safe: a current span that is NOT a LangChain node span (the
    enclosing turn root — the instrumentor not attached) is never ended:
    ending it would truncate the whole turn trace. The wait then stays inside
    the current span (pre-fix behavior)."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    root_span = _NodeFakeSpan("ava-agent-42")
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: root_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert root_span.ended is False
    assert tracer.opened == []


def test_claim_idle_wait_span_skips_non_recording_span(
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-recording current span (sampler dropped it / no real span open)
    is not ended and no idle-wait span is opened — the helper only acts on a
    real recording node span."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    node_span = _NodeFakeSpan("execute_task claim", recording=False)
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: node_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert node_span.ended is False
    assert tracer.opened == []


# --- trace v2: content stripping --------------------------------------------


def _otlp_with_attrs(attrs: dict[str, str]) -> dict:
    """Build a minimal OTLP export request with one span carrying the given
    attributes (keys -> string values)."""
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "execute_task after_init",
                                "attributes": [
                                    {"key": k, "value": {"stringValue": v}}
                                    for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_strip_content_removes_llm_content_keeps_metadata():
    """_strip_content_attributes removes gen_ai.task.input/output,
    traceloop.entity.input/output and messages-like keys; chain metadata and
    status survive."""
    from shared.trace import _strip_content_attributes

    otlp = _otlp_with_attrs(
        {
            "gen_ai.task.input": '{"inputs": {"messages": ["you are ava..."]}}',
            "gen_ai.task.output": "the full completion...",
            "traceloop.entity.input": '{"inputs": {...}}',
            "traceloop.entity.output": '{"outputs": {...}}',
            "messages": "[...]",
            "system_instructions": "[...]",
            "gen_ai.input.messages": "[...]",
            "gen_ai.output.messages": "[...]",
            "traceloop.association.properties.agent_id": "238",
            "traceloop.association.properties.langgraph_path": "after_init",
            "gen_ai.task.status": "success",
            "gen_ai.operation.name": "execute_task",
            "gen_ai.task.id": "019fd0ec-...",
            "session.id": "238",
        }
    )
    _strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert keys == {
        "traceloop.association.properties.agent_id",
        "traceloop.association.properties.langgraph_path",
        "gen_ai.task.status",
        "gen_ai.operation.name",
        "gen_ai.task.id",
        "session.id",
    }


def test_strip_content_size_guard_drops_huge_strings():
    """The size guard drops any single attribute whose string payload exceeds
    _MAX_ATTR_STRING_CHARS even when the key is not a known content key — a
    future instrumentor-invented content key cannot leak megabytes back."""
    from shared.trace import _MAX_ATTR_STRING_CHARS, _strip_content_attributes

    big = "x" * (_MAX_ATTR_STRING_CHARS + 1)
    otlp = _otlp_with_attrs(
        {
            "traceloop.association.properties.agent_id": "238",
            "mystery.new.content.key": big,
        }
    )
    _strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert keys == {"traceloop.association.properties.agent_id"}


def test_strip_content_removes_event_attributes_too():
    """Content attributes nested under span events (the use_attributes=False
    path) are stripped as well."""
    from shared.trace import _strip_content_attributes

    otlp: dict[str, Any] = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "chat",
                                "attributes": [
                                    {
                                        "key": "gen_ai.task.status",
                                        "value": {"stringValue": "success"},
                                    }
                                ],
                                "events": [
                                    {
                                        "name": "gen_ai.input",
                                        "attributes": [
                                            {
                                                "key": "gen_ai.input.messages",
                                                "value": {"stringValue": "[...]"},
                                            },
                                            {
                                                "key": "gen_ai.usage.input_tokens",
                                                "value": {"intValue": "42"},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    _strip_content_attributes(otlp)
    span = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ev_keys = {kv["key"] for kv in span["events"][0]["attributes"]}
    assert ev_keys == {"gen_ai.usage.input_tokens"}


def test_exporter_posts_stripped_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """OtlpJsonHttpSpanExporter strips content attributes before the POST —
    stripped here, content never reaches the sidecar, the mirror or Tempo
    (defensive layer 2)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType]
    )

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318"))
    )
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("ava-agent-7") as root:
        root.set_attribute("session.id", "7")
        root.set_attribute("gen_ai.task.input", "secret prompt")
        root.set_attribute("traceloop.association.properties.agent_id", "7")
    provider.shutdown()
    wire = b"".join(posts)
    assert b"secret prompt" not in wire  # content stripped before the wire
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(posts[0])
    keys = {
        kv["key"]
        for rs in MessageToDict(req)["resourceSpans"]
        for ss in rs["scopeSpans"]
        for sp in ss["spans"]
        for kv in sp.get("attributes", [])
    }
    assert "traceloop.association.properties.agent_id" in keys  # metadata survives


def test_exporter_strip_opt_out_keeps_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """trace_strip_content=False opts the wire back into full content
    (benchmarks that genuinely want prompts in Tempo/mirror)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", False)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType]
    )

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318"))
    )
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("root"):
        pass
    provider.shutdown()

    assert posts


# --- trace v2: file governance -----------------------------------------------


def test_enforce_dir_cap_deletes_oldest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_enforce_dir_cap deletes oldest files until the directory fits the cap."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    # 4 files of 1 MB each: spans-20260101-1 .. spans-20260104-1
    for day in ("20260101", "20260102", "20260103", "20260104"):
        (tmp_path / f"spans-{day}-1.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)  # cap 2 MB -> delete 2 oldest
    assert removed == 2
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-20260103-1.jsonl", "spans-20260104-1.jsonl"]


def test_enforce_dir_cap_noop_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Under the cap nothing is deleted; non-positive cap disables entirely."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-1.jsonl").write_bytes(b"x" * 10)
    assert _enforce_dir_cap(max_mb=100) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1
    assert _enforce_dir_cap(max_mb=0) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1


def test_enforce_dir_cap_active_file_sorts_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The ACTIVE spans.jsonl (no day stamp) sorts last — the cap prune never
    deletes the file the collector is appending to."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-1.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-2026-01-02T03-04-05.000.jsonl").write_bytes(b"x" * (1024 * 1024))
    active = tmp_path / "spans.jsonl"
    active.write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-2026-01-02T03-04-05.000.jsonl", "spans.jsonl"], (
        "the active file must survive a cap prune"
    )


def test_disk_watermark_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_disk_watermark_exceeded compares the data-disk usage fraction against
    the watermark; >= 1.0 disables the guard."""
    from shared.trace import _disk_watermark_exceeded

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "shared.trace_mirror.shutil.disk_usage",
        lambda _p: SimpleNamespace(used=50 * 4096, total=1000 * 4096, free=950 * 4096),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _disk_watermark_exceeded(0.9) is False
    assert _disk_watermark_exceeded(0.01) is True
    assert _disk_watermark_exceeded(1.0) is False  # guard disabled
    assert _disk_watermark_exceeded(2.0) is False


def test_initialize_relief_pass_runs_when_disk_over_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The bounded-disk pass (gzip / retention / cap) runs BEFORE the
    watermark guard: an over-watermark disk still gets its relief pass — the
    stale segment is pruned, the gzip and cap legs are invoked, and recording
    itself is skipped (auto-degrade, watermark guard)."""
    from shared.trace import initialize_tracing

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    monkeypatch.setattr("shared.config.settings.observability.trace_retention_days", 14)
    monkeypatch.setattr("shared.config.settings.observability.trace_max_dir_mb", 2048)
    # Disk OVER the watermark: recording must be skipped...
    monkeypatch.setattr("shared.trace._disk_usage", lambda: (0.99, 10 * 1024**3))
    old = tmp_path / "spans-20200101-1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    # Spy on the gzip and cap legs (the prune leg is exercised for real): all
    # three must be reached before the watermark guard returns. The cap spy
    # records the setting it was invoked with.
    gzip_calls: list[int] = []
    cap_calls: list[int] = []

    def _spy_gzip() -> int:
        gzip_calls.append(1)
        return 0

    def _spy_cap(max_mb: int) -> int:
        cap_calls.append(max_mb)
        return 0

    monkeypatch.setattr("shared.trace._gzip_old_mirror", _spy_gzip)
    monkeypatch.setattr("shared.trace._enforce_dir_cap", _spy_cap)
    init_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "traceloop.sdk.Traceloop.init",
        lambda **kw: init_calls.append(kw),  # pyright: ignore[reportUnknownArgumentType]
    )

    initialize_tracing()

    # ...but the relief pass still ran: the stale segment is pruned, the gzip
    # and cap legs were invoked, and recording itself was skipped.
    assert not old.exists()
    assert gzip_calls == [1]
    assert cap_calls == [2048]  # the cap leg ran, with the configured cap
    assert init_calls == []


def test_initialize_sets_trace_content_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """initialize_tracing forces TRACELOOP_TRACE_CONTENT=false before
    Traceloop.init when strip_content is on."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.delenv("TRACELOOP_TRACE_CONTENT", raising=False)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing()
    _wait_init_resolved()

    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "false"
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_initialize_skips_when_collector_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Local collector not answering at init -> recording stays off, no
    Traceloop.init, and a warning event carries the endpoint (the same
    init-time tradeoff the events exporter makes)."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "shared.trace.endpoint_reachable",
        lambda _e: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    _under_watermark(monkeypatch)
    # This test exercises the skip contract, not the daemon retry loop (that
    # is test_collector_unreachable_retries_once_until_init_succeeds); without
    # the stub the daemon retry thread (300s sleep) leaks across tests.
    monkeypatch.setattr("shared.trace._start_collector_retry", lambda: None)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    warned: list[tuple] = []
    monkeypatch.setattr(
        "shared.trace.logger.warning",
        lambda *a, **kw: warned.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    initialize_tracing()

    assert calls == []
    assert trace_mod._state["initialized"] is False
    assert warned
    attrs = warned[0][1]
    assert attrs.get("action") == "recording_disabled_collector_unreachable"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("endpoint") == "http://127.0.0.1:4318"  # pyright: ignore[reportUnknownMemberType]


def test_initialize_skips_when_disk_over_watermark(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Disk over watermark: recording stays off, no Traceloop.init, and a
    warning telemetry event is emitted carrying the measured numbers."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.trace._disk_usage", lambda: (0.951, 2 * 1024**3))
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    warned: list[tuple] = []
    monkeypatch.setattr(
        "shared.trace.logger.warning",
        lambda *a, **kw: warned.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    initialize_tracing()

    assert calls == []
    assert trace_mod._state["initialized"] is False
    assert warned, "a degradation warning must be logged"
    attrs = warned[0][1]
    assert attrs.get("event") == "trace"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("action") == "recording_disabled_disk_watermark"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("usage_fraction") == 0.951  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("free_gb") == 2.0  # pyright: ignore[reportUnknownMemberType]


def test_enforce_dir_cap_sorts_by_numeric_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Same-day files must sort by NUMERIC pid, not string: name order would
    prune `...-1000` before `...-999`, deleting a newer file (audit 2026-08-08
    P1 — the string order also made a co-located agent's actively-written
    mirror a deletion target)."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-999.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-20260101-1000.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=1)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-20260101-1000.jsonl"], (
        "numeric pid order must delete the older pid-999 file, not pid-1000"
    )


def test_enforce_dir_cap_survives_peer_prune(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A file vanishing between glob and stat — a peer agent pruning the shared
    traces dir concurrently — must not raise out of the boot path (audit
    2026-08-08 P1: two bare p.stat() calls killed an agent start with
    FileNotFoundError)."""
    from pathlib import Path

    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    for day in ("20260101", "20260102", "20260103"):
        (tmp_path / f"spans-{day}-1.jsonl").write_bytes(b"x" * (1024 * 1024))

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self):
        calls["n"] += 1
        if calls["n"] == 2:  # the middle file is gone by the time we stat it
            raise FileNotFoundError
        return real_stat(self)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(Path, "stat", flaky_stat)  # pyright: ignore[reportUnknownArgumentType]
    removed = _enforce_dir_cap(max_mb=1)  # must not raise
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    # the vanished file counted as 0 bytes; the sweep continued past it and
    # deleted the oldest surviving file
    assert remaining == ["spans-20260102-1.jsonl", "spans-20260103-1.jsonl"]
