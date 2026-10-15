from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

import ava
from ava.sdk_surface import install, metering, process_context
from ava.shell.coding_tools._common import owner_terminated
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.api_contracts.agent_contract import AgentStatus
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkMember
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


def test_real_shell_owner_probe_counts_its_public_fanout(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    def terminated(_agent_id: int) -> AgentStatus:
        return AgentStatus.TERMINATED

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    sampling = call_policy.SamplingPolicyOwner()
    monkeypatch.setattr(
        ava.agents,
        "get_status",
        metering.make_recorder(terminated, "agents.get_status", sampling),
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
    process_context.bind_context(
        replace(
            previous or AvaContext(),
            sdk_calls=tally,
            clients=ClientSet(pipeline_factory=emitted_events.producer),
        )
    )
    try:
        assert cast(Callable[[int], bool], ava.self.owner_probe_test)(41)
    finally:
        if previous is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(previous)
    assert tally.snapshot() == {"agents.get_status": 1, "self.owner_probe_test": 1}
    assert [event.attributes["fn"] for event in emitted_events] == [
        "agents.get_status",
        "self.owner_probe_test",
    ]
