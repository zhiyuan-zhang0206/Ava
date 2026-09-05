"""Borrowed identities and plugin state honor consent, TTL and checkpoint ownership."""

# ruff: noqa: S106 — fixture-only credential

from __future__ import annotations

from typing import Annotated, Any

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from ava import _boot, external
from ava._external_state import decode_plugin_delta, encode_plugin_delta
from shared.config.turn_view import turn_settings


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
