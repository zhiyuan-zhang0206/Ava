"""Tests for base.telemetry.tracing — local OTLP-JSON span recording (no network)."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.telemetry import tracing as tracing_mod
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tracing import (
    OtlpJsonHttpSpanExporter,
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
    tracing_mod.shutdown(timeout=_THREAD_DRAIN_TIMEOUT_S)
    retry_thread = tracing_mod._state["retry_thread"]
    if isinstance(retry_thread, threading.Thread):
        retry_thread.join(timeout=_THREAD_DRAIN_TIMEOUT_S)
    arm_thread = tracing_mod._state["arm_thread"]
    if isinstance(arm_thread, threading.Thread) and arm_thread.is_alive():
        init_resolved = tracing_mod._state["init_resolved"]
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
def _reset_init_flag(monkeypatch: pytest.MonkeyPatch):
    """Reset trace-init and retry-loop state (and the key arming writes) between tests."""
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "")  # arming sets it; teardown deletes it
    _assert_no_background_trace_threads()
    gate = telemetry_otlp.observability_export_allowed
    gate.cache_clear()
    tracing_mod._state.clear()
    tracing_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
        arm_thread=None,
        init_resolved=threading.Event(),
        arm_failed=False,
        timeout_reported=False,
    )
    gate.cache_clear()
    yield
    tracing_mod._state["initialized"] = True
    _drain_background_trace_threads()
    tracing_mod._state.clear()
    tracing_mod._state.update(
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
    resolved = tracing_mod._state.get("init_resolved")
    assert isinstance(resolved, threading.Event)
    assert resolved.wait(timeout=timeout), "background trace arming did not resolve"


@pytest.fixture(autouse=True)
def _collector_up(monkeypatch: pytest.MonkeyPatch):
    """The local-collector preflight must pass by default — the tests exercise
    the exporter/init logic, not the network probe (which is covered by its own
    telemetry_otlp tests)."""
    monkeypatch.setattr(
        "base.telemetry.tracing.endpoint_reachable",
        lambda _e: True,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_disabled_returns_early(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, initialize_tracing returns None and does not init the SDK."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", False)
    assert initialize_tracing() is None
    assert tracing_mod._state["initialized"] is False


def _under_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk usage below the watermark: initialize_tracing's auto-degrade guard
    must not skip recording in tests (the dev disk can be >90% full and would
    otherwise make every init-path test environment-dependent)."""
    monkeypatch.setattr("base.telemetry.tracing._disk_usage", lambda: (0.1, 100 * 1024**3))


def test_enabled_inits_traceloop_with_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """trace_enabled=True: Traceloop.init is handed an OtlpJsonHttpSpanExporter
    pointed at the LOCAL collector as the sole exporter (no api_endpoint/api_key
    network sink), batch on, traceloop's own telemetry off, plus the instruments
    set covering Anthropic/OpenAI/LangChain/Google."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    monkeypatch.setattr(tracing_mod, "cluster_label", lambda: ".ava-test")
    monkeypatch.setattr("base.telemetry.observability.production_identity", lambda: False)
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

    assert tracing_mod._state["initialized"] is True


def test_gateway_trace_recording_skips_without_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True)
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp.observability_export_allowed.cache_clear()
    calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", record_init)

    initialize_tracing()

    assert calls == []
    assert tracing_mod._state["initialized"] is False


def test_gateway_trace_recording_arms_with_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "lgtm-host").touch()
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True)
    monkeypatch.setattr("base.telemetry.observability.production_identity", lambda: True)
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path / "traces")
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp.observability_export_allowed.cache_clear()
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
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    def _raise(**_kw):
        raise RuntimeError("boom: init failed")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _raise)  # pyright: ignore[reportUnknownArgumentType]
    warnings: list[tuple] = []
    monkeypatch.setattr(
        "base.telemetry.tracing.logger.warning",
        lambda *a, **kw: warnings.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    initialize_tracing()  # must not raise: the failure is the arm thread's
    _wait_init_resolved()

    assert tracing_mod._state["initialized"] is False
    assert tracing_mod._state["arm_failed"] is True
    assert warnings
    attrs = warnings[0][1]
    assert attrs.get("action") == "recording_init_failed"  # pyright: ignore[reportUnknownMemberType]


def test_arm_failure_blocks_rearming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One arm attempt per process: after a failed init, a later call (e.g. a
    collector-retry re-entry) must NOT run Traceloop.init again — the
    TracerWrapper singleton would fake-succeed without the instrumentors."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    calls: list[int] = []

    def _fail_then_record(**_kw: object) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _fail_then_record)

    initialize_tracing()
    _wait_init_resolved()
    assert tracing_mod._state["arm_failed"] is True

    initialize_tracing()  # must be a no-op, not a second arming attempt
    assert calls == [1]
    assert tracing_mod._state["initialized"] is False


def test_idempotent_second_call_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Second call within the same process does not re-initialize."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
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
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("base.telemetry.tracing.COLLECTOR_RETRY_INTERVAL_S", 0.1)
    _under_watermark(monkeypatch)

    reachable = iter((False, False, True))
    attempts: list[bool] = []

    def endpoint_reachable(_endpoint: str) -> bool:
        result = next(reachable)
        attempts.append(result)
        return result

    monkeypatch.setattr("base.telemetry.tracing.endpoint_reachable", endpoint_reachable)
    warnings: list[str] = []

    def capture_warning(message: str, *_args: object, **_kwargs: object) -> None:
        warnings.append(message)

    monkeypatch.setattr(tracing_mod.logger, "warning", capture_warning)
    initialized = threading.Event()

    def init_traceloop(**_kwargs: object) -> None:
        initialized.set()

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", init_traceloop)

    initialize_tracing()
    retry_thread = tracing_mod._state["retry_thread"]
    assert isinstance(retry_thread, threading.Thread)

    initialize_tracing()
    assert tracing_mod._state["retry_thread"] is retry_thread
    assert initialized.wait(timeout=1.0)
    retry_thread.join(timeout=0.5)
    _wait_init_resolved()

    assert attempts == [False, False, True]
    assert warnings == ["trace recording disabled — local OTel collector not answering"]
    assert tracing_mod._state["initialized"] is True
    assert tracing_mod._state["collector_offline_reported"] is False
    assert not retry_thread.is_alive()


def test_arming_runs_off_the_caller_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The heavy part never blocks the boot path: initialize_tracing returns
    while Traceloop.init is still pending (the mock blocks until released),
    and ensure_init_resolved() is what the use sites wait on."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        assert release.wait(timeout=5.0), "test released the arm thread"

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing()  # returns immediately — the mock is still blocked
    assert entered.wait(timeout=5.0), "arm thread must have started"
    assert tracing_mod._state["initialized"] is False  # boot already returned; init pending

    release.set()
    _wait_init_resolved()
    assert tracing_mod._state["initialized"] is True


def test_teardown_drains_pending_arm_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A test body that returns while the arm thread is still in flight must
    not leave a zombie behind: its delayed Traceloop.init would then land in
    the NEXT test's monkeypatched lambda (counted as a second init — the
    #1065 delta attempt 1 flake). This test only has to leave one in flight;
    the _reset_init_flag teardown has to drain it, and the setup boundary
    check turns a leftover into a deterministic failure."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        release.wait(timeout=1.0)

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)

    initialize_tracing()
    arm_thread = tracing_mod._state["arm_thread"]
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
    from base.telemetry.tracing import _arm_tracing

    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    tracing_mod._state["initialized"] = True
    tracing_mod._state["arm_failed"] = False
    _arm_tracing("http://127.0.0.1:4318")
    assert calls == []

    tracing_mod._state["initialized"] = False
    tracing_mod._state["arm_failed"] = True
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
    from base.telemetry.tracing import _arm_tracing

    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
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
    tracing_mod._state.clear()
    tracing_mod._state.update(
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
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = False
    release = threading.Event()
    arm = threading.Thread(target=release.wait, daemon=True, name="fake-arm")
    tracing_mod._state["arm_thread"] = arm
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

    tracing_mod._state["initialized"] = True
    tracing_mod._state["init_resolved"].set()
    t.join(timeout=5)
    assert entered == ["open"]  # opened only after the arm resolved
    assert not t.is_alive()
    release.set()
    arm.join(timeout=5)


def test_ensure_init_resolved_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung arm must not hang the first turn forever: the wait is bounded,
    logs once, and later calls skip the wait entirely."""
    from base.telemetry.tracing import ensure_init_resolved

    release = threading.Event()
    arm = threading.Thread(target=release.wait, daemon=True, name="fake-arm")
    tracing_mod._state["arm_thread"] = arm
    arm.start()
    monkeypatch.setattr(tracing_mod, "_INIT_RESOLVED_TIMEOUT_S", 0.05)
    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_warning(*a: object, **kw: object) -> None:
        warnings.append((a, kw))

    monkeypatch.setattr("base.telemetry.tracing.logger.warning", _capture_warning)

    ensure_init_resolved()  # times out -> one warning
    ensure_init_resolved()  # remembered -> instant return, no second warning

    assert len(warnings) == 1
    assert warnings[0][1]["action"] == "init_resolved_timeout"
    assert tracing_mod._state["timeout_reported"] is True
    release.set()
    arm.join(timeout=5)


# --- OtlpJsonHttpSpanExporter ---------------------------------------------------


# --- turn_span placeholder-root export timing (#1964) ------------------------------


def _capture_otlp_json_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes, dict]]:
    """Route `httpx.post` into a list so exported OTLP/JSON batches can be decoded."""
    posts: list[tuple[str, bytes, dict]] = []

    def _post(url, *, content, headers, timeout):
        posts.append((url, content, headers))  # pyright: ignore[reportUnknownMemberType]
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType]
    return posts


def _spans_received(posts: list[tuple[str, bytes, dict]]) -> list[Any]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    requests = [ExportTraceServiceRequest.FromString(raw) for _url, raw, _h in posts]
    return [
        span
        for req in requests
        for rs in req.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    ]


def _assert_turn_root_exported_at_start(spans: list[Any]) -> Any:
    assert len(spans) == 1, f"root must export at turn start, got {len(spans)}"
    root = spans[0]
    assert root.name == "ava-agent-7"
    attrs = {kv.key: kv.value.string_value or kv.value.int_value for kv in root.attributes}
    assert attrs["session.id"] == "7"
    assert attrs["ava.turn"] == 3
    assert root.end_time_unix_nano > 0, "placeholder root must be ended when exported"
    return root


def _assert_root_once_with_child_parented_under_it(spans: list[Any], root: Any) -> None:
    roots = [s for s in spans if not s.parent_span_id]
    assert len(roots) == 1, f"root must be exported exactly once, got {len(roots)}"
    assert roots[0].span_id == root.span_id
    children = [s for s in spans if s.parent_span_id]
    assert len(children) == 1
    assert children[0].parent_span_id == root.span_id
    assert children[0].trace_id == root.trace_id


# --- retention prune ---------------------------------------------------------


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


# --- trace v2: file governance -----------------------------------------------


__all__ = ["_FakeTracer", "_NodeFakeSpan"]
