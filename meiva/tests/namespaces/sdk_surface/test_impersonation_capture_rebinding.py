from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.impersonation import history as history
from base.agents.impersonation import manifest as capture
from base.agents.impersonation.manifest import (
    capture_local_event,
    close_local_participant_admission,
    seal_local_participant,
)
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.agents.tests.capture_inputs import capture_rows, capture_state, open_capture, sdk_event
from base.agents.tests.capture_inputs import lease as lease
from base.agents.tests.capture_inputs import owner as owner
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.skills.tree import Skill
from base.telemetry import Event
from meiva.sdk_surface import metering, process_context


class _CaptureOwner:
    def __init__(self, gate: capture.LocalCaptureGate) -> None:
        self.gate = gate

    def admit_sdk_call(self) -> capture.LocalCaptureAdmission | None:
        admission = self.gate.admit()
        if admission is None:
            raise RuntimeError("external attachment is closing")
        return admission

    def capture_local_event(self, event: Event) -> Event:
        return capture_local_event(event, gate=self.gate)


async def test_concurrent_calls_capture_the_original_gate_after_context_rebind(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    previous = open_capture(
        lease, owner.agent_id, "concurrent-original", database_gate=database_gate
    )
    current = open_capture(
        lease, owner.agent_id, "concurrent-replacement", database_gate=database_gate
    )
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)

    entered = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def held() -> None:
        nonlocal started
        started += 1
        if started == 2:
            entered.set()
        await release.wait()

    namespace = SimpleNamespace(held=held, __all_for_meiva__=["held"])
    monkeypatch.setattr(meiva, "receipt_probe", namespace, raising=False)
    monkeypatch.setattr(meiva, "__all_for_meiva__", [*meiva.__all_for_meiva__, "receipt_probe"])

    def _policy_for_test(
        create_capture_owner: call_policy.SamplingPolicyOwner,
    ) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    prior = getattr(meiva, "context", None)
    tally = SdkCallTally()
    context = MeivaContext(
        identity=AgentIdentity(owner.agent_id, True),
        sdk_calls=tally,
        sdk_capture=_CaptureOwner(previous_gate),
    )
    process_context.bind_context(context)
    ledger = metering.install(call_policy.SamplingPolicyOwner())
    calls = [asyncio.create_task(namespace.held()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert previous_gate.in_flight == 2
        assert not close_local_participant_admission(previous_gate, timeout=0)
        process_context.bind_context(replace(context, sdk_capture=_CaptureOwner(current_gate)))
        release.set()
        await asyncio.gather(*calls)
        assert previous_gate.in_flight == current_gate.in_flight == 0
        assert capture_state(db_conn, previous) == ("sealed",)
        assert capture_state(db_conn, current) == ("open",)
        assert capture_rows(db_conn, lease["id"], previous.source_key) == 2
        assert capture_rows(db_conn, lease["id"], current.source_key) == 0
        assert tally.snapshot() == {"receipt_probe.held": 2}
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        metering.uninstall(ledger)
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)
        close_local_participant_admission(current_gate, timeout=0)
        seal_local_participant(current_gate)


def test_sdk_skill_read_captures_borrowed_audit_source_explicitly(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    database_gate: ProcessDbGate,
) -> None:
    from base.agents.context.identity import ExternalLease

    participant = open_capture(
        lease, owner.agent_id, "explicit-skill-audit", database_gate=database_gate
    )
    gate = capture.LocalCaptureGate(participant)
    (tmp_path / "SKILL.md").write_text("# Capture test\n")
    skill = Skill(
        name="capture_test",
        description="test",
        path=str(tmp_path),
        namespace=(),
    )
    monkeypatch.setattr(meiva.skills, "names", lambda: [skill])
    prior = getattr(meiva, "context", None)
    borrowed = ExternalLease(
        agent_id=owner.agent_id,
        validate=lambda: owner.agent_id,
        config=lambda: None,
    )
    clients = ClientSet(database=lambda: database)
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(None, True, lease=borrowed),
            sdk_capture=_CaptureOwner(gate),
            clients=clients,
        )
    )
    try:
        assert "# Capture test" in meiva.files.read(str(tmp_path / "SKILL.md"))
        rows = history.entries(participant.lease_id, db_conn)
        audits = [row for row in rows if row["kind"] == "api_event"]
        assert capture_rows(db_conn, lease["id"], participant.source_key) == 1
        assert len(audits) == 1
        event = audits[0]["payload"]
        assert event["event_name"] == "skill_invoked" and event["source"] == "self"
        assert event["attributes"]["skill"] == "capture_test"
        assert (
            event["attributes"]["impersonation_session"]
            == f"{owner.agent_id}:{lease['session_id']}"
        )
        assert close_local_participant_admission(gate, timeout=0)
        seal_local_participant(gate)
        assert capture_state(db_conn, participant) == ("sealed",)
    finally:
        clients.close()
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)


def test_known_capture_database_outage_retains_the_failed_receipt_recovery(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    participant = open_capture(
        lease, owner.agent_id, "known-capture-outage", database_gate=database_gate
    )
    gate = capture.LocalCaptureGate(participant)

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg.OperationalError("temporary connection loss")

    monkeypatch.setattr(capture, "append_source_event", unavailable)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert admission is not None
        tagged = admission.capture(sdk_event(owner.agent_id, "known-outage"))
        assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
        assert not close_local_participant_admission(gate, timeout=0)
    assert gate.in_flight == 0
    assert capture_state(db_conn, participant) == ("failed",)
    assert capture_rows(db_conn, lease["id"], participant.source_key) == 0
    assert (
        history.resolve(
            Database.from_settings(gate=database_gate),
            owner.agent_id,
            0,
            local_machine=local_machine,
        )["events_completed_at"]
        is None
    )
