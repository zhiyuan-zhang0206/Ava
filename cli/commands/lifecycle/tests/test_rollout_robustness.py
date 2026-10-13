"""Rollout robustness — defects a live 2026-07-28 rollout exposed.

Each of these is a regression test for something the rollout did silently:
- the fan-out dropped a probe-live host because of a stale `machines.stopped_at`,
  and reported a bare count that hid it;
- the roster showed that same host `online` throughout, so the two sources of
  truth disagreed with nothing to see;
- the updater's recovery branch could only fire on a checkout/sync failure, so a
  failed `ava restart` reached no fallback at all.

No cluster is required: the `machines` reads and the ops probe are stubbed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import base.deploy.maintenance.admission as maintenance_admission
import cli.commands._repo as _repo_commands
import cli.commands.lifecycle._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.lifecycle.start as _start_commands
import cli.commands.lifecycle.stop as _stop_commands
from agent.ownership.hosted import admit_hosted_runtime
from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.deploy.maintenance.state import MaintenancePhase
from base.deploy.state import host_deploy_state
from base.events.live.bus import EventBus
from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.telemetry import EventPipeline
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from ops import agent_pause
from ops.cluster_status import pause
from tests.factories.maintenance import WHEN, maintenance_agent
from tests.factories.maintenance import isolate as isolate
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── Defect 1: the fan-out reconciled against a live probe ───────────────────


# ─── Defect 1 (visibility): the roster names the disagreement ────────────────


def test_roster_flags_a_live_host_that_carries_a_stop_marker() -> None:
    """`online` here is what hid the exclusion: the roster's own source of truth
    (a live probe) contradicted the fan-out's (the marker) with nothing to see."""
    from cli.commands.cluster.control import status_cell

    stopped = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"
    # the other three verdicts are unchanged
    assert status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert status_cell(online=True, identity_mismatch=True, stopped_at=stopped) == "MISMATCH"


# ─── Defect 3: declined restart vs failed restart ────────────────────────────


def test_declined_restart_reports_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A preflight refusal stops nothing, so the host is still serving. It must be
    distinguishable from a failure after the stop — the updater shell branches on
    exactly this code to decide whether to run `ava start`."""

    stopped: list[bool] = []

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 1

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "do_stop", lambda *_a, **_k: stopped.append(True) or 0)  # type: ignore[func-returns-value]

    def release_pause(*, database_factory: DatabaseFactory, machine: MachineIdentity) -> None:
        assert database_factory is operator_database
        assert machine is command_runtime.machine

    monkeypatch.setattr(_stop_commands, "release_self_heal_pause", release_pause)

    assert (
        _stop_commands.cmd_restart(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == RESTART_DECLINED_EXIT_CODE
    )
    assert stopped == []  # validate-before-kill: nothing was taken down


def test_failed_restart_after_the_stop_is_not_reported_as_declined(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Once the stop has happened the host may be DOWN, so its code must NOT be the
    one the updater treats as "still serving"."""

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def start_result(
        operation: object,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
        **kwargs: object,
    ) -> int:
        assert machine is command_runtime.machine
        assert database_factory is operator_database
        assert kwargs["producer"] is operator_pipeline
        return 1

    monkeypatch.setattr(_start_commands, "cmd_start_body", start_result)

    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc != 0
    assert rc != RESTART_DECLINED_EXIT_CODE


def _paused_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `release_self_heal_pause`'s posture read answer `paused` — the pause
    state it heals (R1 old-signal sweep, PR5: the posture row replaced the
    `cluster_paused` file)."""
    from base.deploy.state.host_deploy_state import HostDeployState

    monkeypatch.setattr(
        host_deploy_state,
        "read",
        lambda _db, *_a, **_k: HostDeployState(  # pyright: ignore[reportUnknownArgumentType]
            machine="test",
            posture="paused",
            updated_at=datetime.now(UTC),
        ),
    )


def test_declined_restart_releases_a_pause_nothing_else_owns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A locally spawned self-heal pauses this host before running `ava restart`. If
    that restart declines, nothing else clears the pause — so a healthy host would
    sit with its restarter killed until an operator noticed."""

    _paused_posture(monkeypatch)
    monkeypatch.setattr(maintenance_admission, "snapshot", lambda: None)
    unpaused: list[bool] = []
    monkeypatch.setattr(
        pause,
        "unpause_local_cluster",
        lambda _db, _bus, *, local_machine: unpaused.append(
            local_machine is command_runtime.machine
        ),  # pyright: ignore[reportUnknownArgumentType]
    )

    _stop_commands.release_self_heal_pause(
        database_factory=operator_database, machine=command_runtime.machine
    )
    assert unpaused == [True]


def test_declined_restart_leaves_a_stop_holds_pause_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A maintenance hold that has entered its stop window owns this pause and
    `ava start` releases it after readiness; unpausing now would reopen the host
    while its services are half stopped."""
    from base.deploy.maintenance.pause_owner import PauseOwnerSnapshot
    from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase

    _paused_posture(monkeypatch)
    held = PauseOwnerSnapshot(
        status="paused",
        holder="stop",
        acquired_at=datetime.now(UTC),
        maintenance=MaintenanceHold(phase=MaintenancePhase.STOPPED),
    )
    monkeypatch.setattr(maintenance_admission, "snapshot", lambda: held)
    unpaused: list[bool] = []
    monkeypatch.setattr(
        pause,
        "unpause_local_cluster",
        lambda _db, _bus, *, local_machine: unpaused.append(
            local_machine is command_runtime.machine
        ),  # pyright: ignore[reportUnknownArgumentType]
    )

    _stop_commands.release_self_heal_pause(
        database_factory=operator_database, machine=command_runtime.machine
    )
    assert unpaused == []


# ─── A restart that fails before any service stops releases its own hold ─────


def _stop_hold() -> str:
    from ops.agent_pause import new_stop_holder

    return new_stop_holder("test-machine")


def _stub_release_effects(monkeypatch: pytest.MonkeyPatch, event_bus: EventBus) -> None:

    def from_settings(_cls: type[EventBus]) -> EventBus:
        return event_bus

    monkeypatch.setattr(EventBus, "from_settings", classmethod(from_settings))
    monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())


@pytest.mark.parametrize("phase", [MaintenancePhase.PREPARING, MaintenancePhase.DRAINING])
def test_failed_restart_before_any_service_stops_releases_its_own_hold(
    monkeypatch: pytest.MonkeyPatch,
    isolate: None,
    database: Database,
    event_bus: EventBus,
    *,
    command_runtime: CommandRuntime,
    phase: MaintenancePhase,
) -> None:
    """The drain-stage failure leaves the hold this restart took (cohort empty or
    half drained), which gates every wake on the machine until someone runs
    `ava start`. Nothing was stopped, so the restart lets go of it: no compensating
    start, no retry."""
    from base.deploy.maintenance import admission, pause_owner
    from base.deploy.maintenance.state import MaintenanceHold

    _stub_release_effects(monkeypatch, event_bus)
    holder = _stop_hold()
    pause_owner.begin_maintenance(holder, WHEN)
    if phase != MaintenancePhase.PREPARING:
        before = admission.require_operation(holder, WHEN).maintenance
        assert before is not None
        pause_owner.change_maintenance(holder, WHEN, before, MaintenanceHold(phase))
    assert admission.held()

    _stop_commands.release_self_heal_pause(
        database_factory=lambda: database, machine=command_runtime.machine
    )

    assert not admission.held()


def test_failed_restart_leaves_a_hold_another_process_took(
    monkeypatch: pytest.MonkeyPatch,
    isolate: None,
    database: Database,
    event_bus: EventBus,
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.deploy.maintenance import admission, pause_owner

    _stub_release_effects(monkeypatch, event_bus)
    monkeypatch.setattr(host_deploy_state, "read", lambda _db, *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    pause_owner.begin_maintenance(f"local-pause:test-machine:{os.getpid() + 1}:{uuid4()}", WHEN)

    _stop_commands.release_self_heal_pause(
        database_factory=lambda: database, machine=command_runtime.machine
    )

    assert admission.held()


async def test_a_drain_failure_in_restart_leaves_the_host_wakeable(
    monkeypatch: pytest.MonkeyPatch,
    isolate: None,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Through `ava restart` itself: the stop leg takes the hold and fails in its
    drain; the command reports the failure and the hold is gone."""
    from base.deploy.maintenance import admission, pause_owner

    _stub_release_effects(monkeypatch, event_bus)

    def passes(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", passes)
    monkeypatch.setattr(_start_readiness_preflight_commands, "preflight_start_readiness", passes)

    def failing_drain(*_args: object, **_kwargs: object) -> int:
        pause_owner.begin_maintenance(_stop_hold(), WHEN)
        return 1

    started: list[bool] = []
    monkeypatch.setattr(_stop_commands, "do_stop", failing_drain)

    def start(*_args: object, **_kwargs: object) -> int:
        started.append(True)
        return 0

    monkeypatch.setattr(_start_commands, "cmd_start_body", start)

    agent = maintenance_agent(db_conn, machine_name=command_runtime.machine.name())

    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=lambda: database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert rc == 1
    assert not admission.held()
    assert started == []  # no compensating start
    # An idle agent is admitted by the next wake instead of being refused by the hold.
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent,
        command_runtime.machine.name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert incarnation is not None
