"""Borrowed identities and plugin state honor consent, TTL and checkpoint ownership."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Event, Thread, Timer
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

import ava
from agent import state as state_module
from ava import external, gateway_client
from ava.external import state
from ava.external.state import apply_plugin_delta, decode_plugin_delta, encode_plugin_delta
from ava.sdk_surface import agent_identity
from ava.sdk_surface import settings as _settings
from ava.sdk_surface.settings import agent_setting
from base import telemetry
from base.config import settings
from base.db import Database
from base.lm.plugin_providers import build_model_catalog
from base.telemetry.otlp.tests.external_flush import paused_otlp_record as paused_otlp_record
from tests.factories.external_attachment import HANDSHAKE_BOUND_S as _HANDSHAKE_BOUND_S
from tests.factories.external_attachment import ExampleMessagesPlugin, ExamplePlugin, ExampleState
from tests.factories.external_attachment import attached_runtime as attached_runtime


def _skip_local_capture(event: telemetry.Event, **_kwargs: Any) -> telemetry.Event:
    """The symbolic-lease harness leaves receipt writes to the real SQL suite."""
    return event


def test_attach_borrows_identity_even_with_explicit_external_profile(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    with external.attach("lease"):
        assert _borrowed_agent_id() == 405
        assert ava.self.AGENT_ID == 405
        assert agent_identity.require_agent_id() == 405
        assert agent_identity.require_actor() == "agent:405"
        assert agent_identity.require_actor() == "agent:405"
        assert agent_setting("llm_model") == "external-test"
    assert _borrowed_agent_id() is None
    assert agent_identity.require_actor() == "external_agent:codex"
    assert not ava.in_exec_turn()


def test_legacy_attachment_never_opens_an_event_receipt(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NULL-version legacy attachment has no event-log database side effect."""

    def unexpected_open(_db: object, _lease_id: str, *, agent_id: int, source_key: str) -> bool:
        del agent_id, source_key
        pytest.fail("legacy attachment opened an event receipt")

    monkeypatch.setattr(
        "base.agents.impersonation.manifest.open_local_participant",
        unexpected_open,
    )

    with external.attach("lease"):
        pass


def test_expiry_blocks_identity_and_plugin_state_before_new_effects(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, staged = attached_runtime
    attachment = external.attach("lease")
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    lease["status"] = "expired"
    for read in (
        lambda: ava.self.AGENT_ID,
        agent_identity.require_actor,
        handle.read,
        lambda: handle.update({"seen": {"too-late"}}),
    ):
        with pytest.raises(RuntimeError, match="expired"):
            read()
    with pytest.raises(RuntimeError, match="expired"):
        attachment.close()
    assert _borrowed_agent_id() is None
    assert not staged


def test_attach_requests_native_plugin_load(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attach path asks for the faces-included plugin load (#2616 review).

    ``load_snapshot`` rebuilds the checkpoint through ``build_agent_state(build_registry())``,
    so a surface-only load silently drops the plugin state fields the lease
    carries. Locks the wiring; the loader's own surface/full split is
    exercised in agent/tests/execution/test_lazy_child_imports.py.
    """
    calls: list[dict[str, Any]] = []

    def spy_loader(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(ava, "ensure_plugins_loaded", spy_loader)
    with external.attach("lease"):
        pass
    assert len(calls) == 1
    assert calls[0]["surface"] is False
    assert calls[0]["config"] is None
    assert set(calls[0]) == {"surface", "config", "clock_factory"}
    assert callable(calls[0]["clock_factory"])


def test_plugin_updates_journal_once_and_next_attachment_sees_them(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    _, _, staged = attached_runtime
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    with external.attach("lease"):
        handle.update({"seen": {"one"}})
        handle.update({"seen": {"two"}})
        assert handle.read().seen == {"native", "one", "two"}
    assert len(staged) == 1
    assert decode_plugin_delta(staged[0], ExampleState) == {"sample__seen": {"one", "two"}}
    with external.attach("lease"):
        assert handle.read().seen == {"native", "one", "two"}
    assert len(staged) == 1


def test_stale_attachment_refuses_sdk_identity_and_removes_identity_on_close(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, _ = attached_runtime
    attachment = external.attach("lease")
    lease["delta_version"] += 1
    with pytest.raises(RuntimeError, match="another attachment"):
        agent_identity.require_actor()
    with pytest.raises(RuntimeError, match="another attachment"):
        attachment.close()
    assert _borrowed_agent_id() is None


def _invalidate_lease(lease: dict[str, Any], invalidated: str) -> str:
    """Invalidate the attached lease; the error text its next use must raise."""
    if invalidated == "expiry":
        lease["status"] = "expired"
        return "expired"
    lease["delta_version"] += 1
    return "another attachment"


def _borrowed_agent_id() -> int | None:
    """The agent id the process's bound context borrows through an attachment, if any."""
    context = getattr(ava, "context", None)
    lease = None if context is None or context.identity is None else context.identity.lease
    return None if lease is None else lease.agent_id


def _assert_detached() -> None:
    """After a detach the process holds no exec slot and no attached config."""
    assert not ava.in_exec_turn()
    assert _settings._attached() is None


def test_attach_refuses_inside_an_exec_turn(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    ava.state = ExampleState(sample__seen={"prior"})
    with pytest.raises(RuntimeError, match="exec turn cannot attach"):
        external.attach("lease")
    assert _borrowed_agent_id() is None


@pytest.mark.parametrize("invalidated", ["expiry", "state_version"])
def test_failed_context_entry_detaches_and_allows_next_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    invalidated: str,
) -> None:
    lease, _, staged = attached_runtime
    attachment = external.attach("lease")
    state_module.PluginStateHandle(ExamplePlugin, "sample").update({"seen": {"unflushed"}})
    reason = _invalidate_lease(lease, invalidated)
    with pytest.raises(RuntimeError, match=reason), attachment:
        pytest.fail("an invalid attachment entered its context")
    assert _borrowed_agent_id() is None
    _assert_detached()
    assert not staged
    attachment.close()  # Already detached; must not retry the failed lease or flush.

    next_lease = {**lease, "id": "next", "status": "active", "delta_version": 0}

    def require_next(_db: Database, lease_id: str, attesting: dict[str, Any]) -> dict[str, Any]:
        assert lease_id == "next"
        assert attesting == {"pid": 777}
        return next_lease

    monkeypatch.setattr(external.control, "require_active", require_next)
    with external.attach("next"):
        assert _borrowed_agent_id() == 405
        assert ava.self.AGENT_ID == 405
    _assert_detached()


def test_concurrent_constructor_fails_before_lease_lookup(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require = external.control.require_active
    calls = count()
    first_lookup = Event()
    continue_lookup = Event()

    def blocked_require(db: Database, lease_id: str, attesting: dict[str, Any]) -> dict[str, Any]:
        if next(calls) == 0:
            first_lookup.set()
            assert continue_lookup.wait(5), "test did not release the first lease lookup"
        return require(db, lease_id, attesting)

    def attach_in_worker() -> None:
        with external.attach("lease"):
            assert _borrowed_agent_id() == 405
            assert ava.self.AGENT_ID == 405

    monkeypatch.setattr(external.control, "require_active", blocked_require)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(attach_in_worker)
        try:
            assert first_lookup.wait(5), "first constructor did not reach the lease lookup"
            with (
                pytest.raises(RuntimeError, match="already has an external attachment"),
                external.attach("lease"),
            ):
                pass
        finally:
            continue_lookup.set()
            first.result(timeout=5)
    assert _borrowed_agent_id() is None
    with external.attach("lease"):
        assert _borrowed_agent_id() == 405
        assert ava.self.AGENT_ID == 405


@pytest.mark.parametrize("failure_at", ["lease", "snapshot"])
def test_constructor_failure_detaches_and_allows_next_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    _, _, staged = attached_runtime

    def fail(_db: object, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("constructor interrupted")

    with monkeypatch.context() as failure_patch:
        if failure_at == "lease":
            failure_patch.setattr(external.control, "require_active", fail)
        else:
            failure_patch.setattr(external, "load_snapshot", fail)
        with pytest.raises(RuntimeError, match="constructor interrupted"):
            external.attach("lease")
    assert _borrowed_agent_id() is None
    _assert_detached()
    assert not staged
    with external.attach("lease"):
        assert _borrowed_agent_id() == 405
        assert ava.self.AGENT_ID == 405


def test_repeated_close_cannot_release_another_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    first = external.attach("lease")
    first.close()
    with external.attach("lease"):
        first.close()
        assert _borrowed_agent_id() == 405
        assert ava.self.AGENT_ID == 405
        with pytest.raises(RuntimeError, match="already has an external attachment"):
            external.attach("lease")


def test_close_rejects_a_new_sdk_effect_before_it_reaches_the_gateway(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Close fences a new SDK call before its gateway effect, not only at detach."""
    from base.agents.impersonation import manifest

    attachment = external.attach("lease")
    participant = manifest.LocalParticipant(
        "lease", attachment.agent_id, 0, "post-close-sdk", database
    )
    attachment._event_gate = manifest.LocalCaptureGate(participant)
    monkeypatch.setattr(manifest, "capture_local_event", _skip_local_capture)
    delivered: list[tuple[int, str]] = []

    def record_send(agent_id: int, *, content: str, source: str) -> None:
        del source
        delivered.append((agent_id, content))

    def new_call_during_close() -> None:
        with pytest.raises(RuntimeError, match="closing"):
            ava.agents.send_message(99, "must not reach gateway")

    def skip_seal(_gate: manifest.LocalCaptureGate) -> None:
        return None

    monkeypatch.setattr(gateway_client, "send_message", record_send)
    monkeypatch.setattr(manifest, "seal_local_participant", skip_seal)
    monkeypatch.setattr(attachment, "flush", new_call_during_close)
    attachment.close()
    assert delivered == []


def test_close_does_not_revoke_an_sdk_call_admitted_before_the_fence(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """An SDK call already inside its metering admission finishes its gateway effect."""
    from base.agents.impersonation import manifest

    attachment = external.attach("lease")
    participant = manifest.LocalParticipant(
        "lease", attachment.agent_id, 0, "pre-close-sdk", database
    )
    attachment._event_gate = manifest.LocalCaptureGate(participant)
    monkeypatch.setattr(manifest, "capture_local_event", _skip_local_capture)
    entered, release = Event(), Event()
    delivered: list[tuple[int, str]] = []

    def held_send(agent_id: int, *, content: str, source: str) -> None:
        del source
        entered.set()
        assert release.wait(_HANDSHAKE_BOUND_S), "close did not release the pre-close SDK call"
        delivered.append((agent_id, content))

    monkeypatch.setattr(gateway_client, "send_message", held_send)

    def skip_seal(_gate: manifest.LocalCaptureGate) -> None:
        return None

    monkeypatch.setattr(manifest, "seal_local_participant", skip_seal)
    worker = Thread(target=lambda: ava.agents.send_message(99, "already admitted"))
    worker.start()
    assert entered.wait(_HANDSHAKE_BOUND_S), "SDK call did not reach its gateway boundary"
    attachment.close()
    release.set()
    worker.join(_HANDSHAKE_BOUND_S)
    assert not worker.is_alive()
    assert delivered == [(99, "already admitted")]


def test_close_delivers_telemetry_when_plugin_flush_fails(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close finally path must run delivery as well as detachment."""
    attachment = external.attach("lease")
    delivered: list[tuple[bool, int | None]] = []

    def fail_flush() -> None:
        raise RuntimeError("plugin journal unavailable")

    monkeypatch.setattr(attachment, "flush", fail_flush)

    def sync_delivery(*, bounded: bool) -> telemetry.DrainResult:
        delivered.append((bounded, _borrowed_agent_id()))
        return telemetry.DrainResult(telemetry.DrainStatus.COMPLETED, telemetry.DrainPhase.DRAIN)

    monkeypatch.setattr(telemetry, "sync", sync_delivery)

    with pytest.raises(RuntimeError, match="plugin journal unavailable"):
        attachment.close()

    assert delivered == [(True, 405)]
    assert _borrowed_agent_id() is None


def test_close_waits_for_a_dequeued_otlp_record_before_force_flush(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    paused_otlp_record: tuple[Event, InMemoryLogRecordExporter],
) -> None:
    """A closing attachment waits for the OTLP worker's already-dequeued tail."""
    release, exporter = paused_otlp_record
    attachment = external.attach("lease")
    timer = Timer(0.1, release.set)
    timer.daemon = True
    timer.start()
    try:
        started = time.monotonic()
        attachment.close()
        assert time.monotonic() - started >= 0.08
        assert len(exporter.get_finished_logs()) == 1
    finally:
        release.set()
        timer.cancel()


def test_receipted_journal_entries_are_not_replayed(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, snapshot, _ = attached_runtime
    snapshot.impersonation_applied = {"lease_id": "lease", "version": 1}
    lease["delta_version"] = 1
    lease["plugin_delta"] = [
        encode_plugin_delta({"messages": [RemoveMessage(id="already-removed")]}, ExampleState)
    ]
    with external.attach("lease"):
        assert ava.state.messages == []


def test_attach_rejects_other_machine_without_binding_identity(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, _ = attached_runtime
    lease["machine"] = "another-runner"
    with pytest.raises(RuntimeError, match="agent machine"):
        external.attach("lease")
    assert _borrowed_agent_id() is None


def test_delta_codec_preserves_sets_and_message_objects(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    delta = {
        "sample__seen": {"a", "b"},
        "messages": [HumanMessage(content="note", id="note"), RemoveMessage(id="old")],
    }
    decoded = decode_plugin_delta(encode_plugin_delta(delta, ExampleState), ExampleState)
    assert decoded == delta
    assert isinstance(decoded["sample__seen"], set)
    assert isinstance(decoded["messages"][1], RemoveMessage)


def test_delta_codec_rejects_framework_state_injection(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    with pytest.raises(ValueError, match="framework core"):
        encode_plugin_delta({"halted": False}, ExampleState)


@pytest.mark.parametrize("boundary", ["encode", "decode", "apply"])
@pytest.mark.parametrize(
    "messages",
    [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content="replacement")],
        [{"type": "remove", "id": REMOVE_ALL_MESSAGES, "content": ""}],
    ],
    ids=["single-marker", "wipe-and-replace", "message-dict"],
)
def test_external_delta_rejects_full_history_reset_before_any_mutation(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    boundary: str,
    messages: Any,
) -> None:
    _, snapshot, _ = attached_runtime
    snapshot.messages = [HumanMessage(content="Native history", id="native")]
    before = snapshot.model_copy(deep=True)
    delta = {"sample__seen": {"must-not-apply"}, "messages": messages}
    with pytest.raises(ValueError, match=r"REMOVE_ALL.*native compaction"):
        if boundary == "encode":
            encode_plugin_delta(delta, ExampleState)
        elif boundary == "decode":
            # A persisted envelope must pass the same check before native replay.
            import base64

            encoding, payload = state._serializer().dumps_typed(delta)
            decode_plugin_delta(
                {"encoding": encoding, "data": base64.b64encode(payload).decode("ascii")},
                ExampleState,
            )
        else:
            apply_plugin_delta(snapshot, delta)
    assert snapshot == before


def test_external_attachment_appends_messages_without_replacing_native_history(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    _, snapshot, staged = attached_runtime
    native_message = HumanMessage(content="Native history", id="native")
    appended_message = HumanMessage(content="External progress", id="external")
    snapshot.messages = [native_message]
    handle = state_module.PluginStateHandle(ExampleMessagesPlugin, "sample")
    with external.attach("lease"):
        handle.update({"messages": [appended_message]})
        assert handle.read().messages == [native_message, appended_message]
    assert len(staged) == 1
    assert decode_plugin_delta(staged[0], ExampleState) == {"messages": [appended_message]}
    assert snapshot.messages == [native_message]
    with external.attach("lease"):
        assert handle.read().messages == [native_message, appended_message]
    assert len(staged) == 1


def test_external_attachment_refuses_to_journal_a_full_history_reset(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    _, snapshot, staged = attached_runtime
    snapshot.messages = [HumanMessage(content="Native history", id="native")]
    attachment = external.attach("lease")
    ava.state_update = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]}
    with pytest.raises(ValueError, match=r"REMOVE_ALL.*native compaction"):
        attachment.close()
    assert not staged
    assert _borrowed_agent_id() is None
    assert snapshot.messages == [HumanMessage(content="Native history", id="native")]


def test_attachment_reuses_clients_and_restores_original_context(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    original = getattr(ava, "context", None)
    assert original is not None
    with external.attach("lease"):
        assert ava.context.clients is original.clients
        assert ava.context.identity is not None
        assert ava.context.identity.lease is not None
        assert ava.context.identity.lease.agent_id == 405
        assert ava.context.sql is ava.DB
        assert ava.context.redis is ava.REDIS
    assert getattr(ava, "context", None) is original


def test_external_controls_stay_out_of_native_prompt(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    from agent.graph.prompt.system_prompt import build_system_prompt
    from base.host.env.agent_slices import AgentSlices
    from base.packages.plugins.extensions import ExtensionRegistry

    prompt = build_system_prompt(
        ExtensionRegistry(()),
        AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        agent_id=None,
        catalog=build_model_catalog(),
    )
    assert "external" not in ava.__all_for_ava__
    assert "ava.external.attach" not in prompt
    assert "## ava.external" not in prompt


def test_close_preserves_flush_error_when_receipt_cleanup_also_fails(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    from base.agents.impersonation import manifest

    attachment = external.attach("lease")
    attachment._event_gate = manifest.LocalCaptureGate(
        manifest.LocalParticipant(
            "lease",
            attachment.agent_id,
            0,
            "close-failure",
            database,
        )
    )
    primary = ValueError("plugin flush failed")
    secondary = RuntimeError("receipt seal failed")

    def fail_flush() -> None:
        raise primary

    def fail_seal(_gate: manifest.LocalCaptureGate) -> None:
        raise secondary

    monkeypatch.setattr(attachment, "flush", fail_flush)
    monkeypatch.setattr(manifest, "seal_local_participant", fail_seal)
    with pytest.raises(ValueError) as raised:
        attachment.close()
    assert raised.value is primary
    assert any("receipt seal failed" in note for note in primary.__notes__)
    assert _borrowed_agent_id() is None


def test_attachment_context_keeps_body_error_primary_when_close_fails(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = external.attach("lease")
    primary = LookupError("body failed")

    def fail_flush() -> None:
        raise RuntimeError("flush failed")

    monkeypatch.setattr(attachment, "flush", fail_flush)
    with pytest.raises(LookupError) as raised, attachment:
        raise primary
    assert raised.value is primary
    assert any("flush failed" in note for note in primary.__notes__)
    assert _borrowed_agent_id() is None


def test_close_reports_unfinished_ordinary_telemetry(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.log import logger

    def unfinished(*, bounded: bool) -> telemetry.DrainResult:
        assert bounded
        return telemetry.DrainResult(telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.DRAIN)

    monkeypatch.setattr(telemetry, "sync", unfinished)
    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        attachment = external.attach("lease")
        attachment.close()
        assert _borrowed_agent_id() is None
    finally:
        logger.remove(sink)
    assert any("ordinary telemetry delivery is unfinished" in message for message in warnings)
    assert all("stay in the JSONL mirror" not in message for message in warnings)
