"""The real shell owner probe meters its nested public SDK entry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

import ava
from ava.sdk_surface import install, metering
from ava.shell.coding_tools._common import owner_terminated
from base.agents import AgentStatus
from base.agents.context import AvaContext
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_telemetry
from base.agents.sdk.tally import SdkCallTally
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkMember


def test_real_shell_owner_probe_counts_its_public_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def emit(fn: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append(fn)

    def terminated(_agent_id: int) -> AgentStatus:
        return AgentStatus.TERMINATED

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    monkeypatch.setattr(sdk_telemetry, "emit", emit)
    sampling = call_policy.SamplingPolicyOwner()
    monkeypatch.setattr(
        ava.agents,
        "get_status",
        metering._make_recorder(terminated, "agents.get_status", sampling),
    )
    registry = ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_members=(SdkMember("self", "owner_probe_test", owner_terminated),),
                ),
            ),
        )
    )
    install.install(registry, sampling=sampling)
    previous = getattr(ava, "context", None)
    tally = SdkCallTally()
    ava.bind_context(replace(previous or AvaContext(), sdk_calls=tally))
    try:
        assert cast(Callable[[int], bool], ava.self.owner_probe_test)(41)
    finally:
        if previous is None:
            ava.unbind_context()
        else:
            ava.bind_context(previous)
    assert tally.snapshot() == {"agents.get_status": 1, "self.owner_probe_test": 1}
    assert calls == ["agents.get_status", "self.owner_probe_test"]
