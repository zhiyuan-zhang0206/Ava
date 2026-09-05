"""Borrowed identities and plugin state honor consent, TTL and checkpoint ownership."""

# ruff: noqa: S106 — fixture-only credential

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Event
from typing import Annotated, Any

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from ava import _boot, external
from ava._external_state import decode_plugin_delta, encode_plugin_delta
from shared.config.turn_view import bind_agent_config, current_agent_config_pins, turn_settings
from shared.plugin_config_view import bind_agent_plugin_config, current_plugin_config_view


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class ExampleState(state_module.BaseAgentState):
    sample__seen: Annotated[set[str], _union] = Field(default_factory=set)


class ExamplePlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


@pytest.fixture
def attached_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    lease: dict[str, Any] = {
        "id": "lease",
        "agent_id": 405,
        "machine": "local-runner",
        "status": "active",
        "delta_version": 0,
        "applied_version": 0,
        "plugin_delta": [],
    }
    staged: list[dict[str, Any]] = []
    snapshot = ExampleState(sample__seen={"native"})
    monkeypatch.setattr(_boot, "_external_identity", None)
    monkeypatch.setattr(_boot, "_agent_id", None)
    monkeypatch.setattr(_boot, "_owns_loop", True)
    monkeypatch.setattr(ava, "state", None)
    monkeypatch.setattr(ava, "state_update", None)
    monkeypatch.setattr(ava, "_ensure_plugins_loaded", lambda: None)
    monkeypatch.setattr(state_module, "AgentState", ExampleState)
    monkeypatch.setattr(state_module, "_BASE_FIELD_DECLARED", {"messages"})
    monkeypatch.setattr(external, "machine_name", lambda: "local-runner")

    def load(_agent_id: int) -> tuple[ExampleState, dict[str, Any], None]:
        return snapshot.model_copy(deep=True), {"llm_model": "external-test"}, None

    monkeypatch.setattr(external, "load_snapshot", load)

    def require(lease_id: str, token: str) -> dict[str, Any]:
        assert (lease_id, token) == ("lease", "credential")
        if lease["status"] != "active":
            raise RuntimeError("lease expired")
        return dict(lease)

    def stage(lease_id: str, token: str, delta: dict[str, Any], *, expected_version: int) -> None:
        require(lease_id, token)
        if expected_version != lease["delta_version"]:
            raise RuntimeError("stale version")
        staged.append(delta)
        lease["plugin_delta"].append(delta)
        lease["delta_version"] += 1

    monkeypatch.setattr(external.control, "require_active", require)
    monkeypatch.setattr(external.control, "merge_plugin_delta", stage)
    return lease, snapshot, staged


def test_attach_borrows_identity_even_with_explicit_external_profile(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    with external.attach("lease", token="credential"):
        assert ava.self.AGENT_ID == 405
        assert _boot.require_agent_id() == 405
        assert _boot.require_actor() == "agent:405"
        assert _boot.default_actor() == "agent:405"
        assert turn_settings.lm.llm_model == "external-test"
    assert _boot._external_identity is None
    assert _boot.require_actor() == "external_agent:codex"
    assert ava.state is None


def test_expiry_blocks_identity_and_plugin_state_before_new_effects(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, staged = attached_runtime
    attachment = external.attach("lease", token="credential")
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    lease["status"] = "expired"
    for read in (
        lambda: ava.self.AGENT_ID,
        _boot.require_actor,
        handle.read,
        lambda: handle.update({"seen": {"too-late"}}),
    ):
        with pytest.raises(RuntimeError, match="expired"):
            read()
    with pytest.raises(RuntimeError, match="expired"):
        attachment.close()
    assert _boot._external_identity is None
    assert not staged


def test_plugin_updates_journal_once_and_next_attachment_sees_them(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    _, _, staged = attached_runtime
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    with external.attach("lease", token="credential"):
        handle.update({"seen": {"one"}})
        handle.update({"seen": {"two"}})
        assert handle.read().seen == {"native", "one", "two"}
    assert len(staged) == 1
    assert decode_plugin_delta(staged[0]) == {"sample__seen": {"one", "two"}}
    with external.attach("lease", token="credential"):
        assert handle.read().seen == {"native", "one", "two"}
    assert len(staged) == 1


def test_stale_attachment_refuses_sdk_identity_and_removes_identity_on_close(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, _ = attached_runtime
    attachment = external.attach("lease", token="credential")
    lease["delta_version"] += 1
    with pytest.raises(RuntimeError, match="another attachment"):
        _boot.require_actor()
    with pytest.raises(RuntimeError, match="another attachment"):
        attachment.close()
    assert _boot._external_identity is None


@pytest.mark.parametrize("invalidated", ["expiry", "state_version"])
def test_failed_context_entry_restores_prior_binding_and_allows_next_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    invalidated: str,
) -> None:
    lease, _, staged = attached_runtime
    prior_state = ExampleState(sample__seen={"prior"})
    prior_update = {"sample__seen": {"pending-before-attachment"}}
    monkeypatch.setattr(ava, "state", prior_state)
    monkeypatch.setattr(ava, "state_update", prior_update)
    with (
        bind_agent_config({"llm_model": "prior-model"}),
        bind_agent_plugin_config({"sample": {"setting": "prior"}}),
    ):
        prior_config = current_agent_config_pins()
        prior_plugin_config = current_plugin_config_view()
        attachment = external.attach("lease", token="credential")
        state_module.PluginStateHandle(ExamplePlugin, "sample").update({"seen": {"unflushed"}})
        if invalidated == "expiry":
            lease["status"] = "expired"
            reason = "expired"
        else:
            lease["delta_version"] += 1
            reason = "another attachment"
        with pytest.raises(RuntimeError, match=reason), attachment:
            pytest.fail("an invalid attachment entered its context")
        assert _boot._external_identity is None
        assert ava.state is prior_state
        assert ava.state_update is prior_update
        assert current_agent_config_pins() is prior_config
        assert current_plugin_config_view() is prior_plugin_config
        assert not staged
        attachment.close()  # Already detached; must not retry the failed lease or flush.

        next_lease = {**lease, "id": "next", "status": "active", "delta_version": 0}

        def require_next(lease_id: str, token: str) -> dict[str, Any]:
            assert (lease_id, token) == ("next", "credential")
            return next_lease

        monkeypatch.setattr(external.control, "require_active", require_next)
        with external.attach("next", token="credential"):
            assert ava.self.AGENT_ID == 405
        assert ava.state is prior_state
        assert ava.state_update is prior_update
        assert current_agent_config_pins() is prior_config
        assert current_plugin_config_view() is prior_plugin_config


def test_concurrent_constructor_fails_before_lease_lookup(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require = external.control.require_active
    calls = count()
    first_lookup = Event()
    continue_lookup = Event()

    def blocked_require(lease_id: str, token: str) -> dict[str, Any]:
        if next(calls) == 0:
            first_lookup.set()
            assert continue_lookup.wait(5), "test did not release the first lease lookup"
        return require(lease_id, token)

    def attach_in_worker() -> None:
        with external.attach("lease", token="credential"):
            assert ava.self.AGENT_ID == 405

    monkeypatch.setattr(external.control, "require_active", blocked_require)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(attach_in_worker)
        try:
            assert first_lookup.wait(5), "first constructor did not reach the lease lookup"
            with (
                pytest.raises(RuntimeError, match="already has an external attachment"),
                external.attach("lease", token="credential"),
            ):
                pass
        finally:
            continue_lookup.set()
            first.result(timeout=5)
    assert _boot._external_identity is None
    with external.attach("lease", token="credential"):
        assert ava.self.AGENT_ID == 405


@pytest.mark.parametrize("failure_at", ["lease", "snapshot"])
def test_constructor_failure_restores_binding_and_allows_next_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    _, _, staged = attached_runtime
    prior_state = ExampleState(sample__seen={"prior"})
    prior_update = {"sample__seen": {"pending-before-attachment"}}
    monkeypatch.setattr(ava, "state", prior_state)
    monkeypatch.setattr(ava, "state_update", prior_update)

    def fail(*_args: Any) -> Any:
        raise RuntimeError("constructor interrupted")

    with (
        bind_agent_config({"llm_model": "prior-model"}),
        bind_agent_plugin_config({"sample": {"setting": "prior"}}),
    ):
        prior_config = current_agent_config_pins()
        prior_plugin_config = current_plugin_config_view()
        with monkeypatch.context() as failure_patch:
            if failure_at == "lease":
                failure_patch.setattr(external.control, "require_active", fail)
            else:
                failure_patch.setattr(external, "load_snapshot", fail)
            with pytest.raises(RuntimeError, match="constructor interrupted"):
                external.attach("lease", token="credential")
        assert _boot._external_identity is None
        assert ava.state is prior_state
        assert ava.state_update is prior_update
        assert current_agent_config_pins() is prior_config
        assert current_plugin_config_view() is prior_plugin_config
        assert not staged
        with external.attach("lease", token="credential"):
            assert ava.self.AGENT_ID == 405


def test_repeated_close_cannot_release_another_attachment(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    first = external.attach("lease", token="credential")
    first.close()
    with external.attach("lease", token="credential"):
        first.close()
        assert ava.self.AGENT_ID == 405
        with pytest.raises(RuntimeError, match="already has an external attachment"):
            external.attach("lease", token="credential")


def test_expired_lease_cannot_dispatch_through_local_mcp(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import mcps

    lease, _, _ = attached_runtime
    dispatched: list[bool] = []

    def local_dispatch(coroutine: Any) -> dict[str, Any]:
        coroutine.close()
        dispatched.append(True)
        return {}

    monkeypatch.setattr(mcps, "_get_remote_client", lambda: None)
    monkeypatch.setattr(mcps, "_run_async", local_dispatch)
    attachment = external.attach("lease", token="credential")
    lease["status"] = "expired"
    try:
        with pytest.raises(RuntimeError, match="expired"):
            mcps._call_raw("example", "side_effect")
        assert not dispatched
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()


def test_mcp_revalidates_lease_before_transport_fallback(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import mcps

    lease, _, _ = attached_runtime

    class FailedDaemon:
        def call_tool(self, *_args: Any) -> dict[str, Any]:
            lease["status"] = "expired"
            raise mcps.MCPConnectError("daemon disconnected")

    monkeypatch.setattr(mcps, "_get_remote_client", FailedDaemon)
    attachment = external.attach("lease", token="credential")
    try:
        with pytest.raises(RuntimeError, match="expired"):
            mcps._call_raw("example", "side_effect")
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()


def test_receipted_journal_entries_are_not_replayed(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, snapshot, _ = attached_runtime
    snapshot.impersonation_applied = {"lease_id": "lease", "version": 1}
    lease["delta_version"] = 1
    lease["plugin_delta"] = [
        encode_plugin_delta({"messages": [RemoveMessage(id="already-removed")]})
    ]
    with external.attach("lease", token="credential"):
        assert ava.state.messages == []


def test_attach_rejects_other_machine_without_binding_identity(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, _ = attached_runtime
    lease["machine"] = "another-runner"
    with pytest.raises(RuntimeError, match="agent machine"):
        external.attach("lease", token="credential")
    assert _boot._external_identity is None


def test_delta_codec_preserves_sets_and_message_objects(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    delta = {
        "sample__seen": {"a", "b"},
        "messages": [HumanMessage(content="note", id="note"), RemoveMessage(id="old")],
    }
    decoded = decode_plugin_delta(encode_plugin_delta(delta))
    assert decoded == delta
    assert isinstance(decoded["sample__seen"], set)
    assert isinstance(decoded["messages"][1], RemoveMessage)


def test_delta_codec_rejects_framework_state_injection(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    with pytest.raises(ValueError, match="framework core"):
        encode_plugin_delta({"halted": False})
