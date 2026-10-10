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

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import cli.commands._repo as _repo_commands
import cli.commands.lifecycle._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.lifecycle.start as _start_commands
import cli.commands.lifecycle.stop as _stop_commands
from base.agents.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.telemetry import EventPipeline
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── Defect 1: the fan-out reconciled against a live probe ───────────────────


# ─── Defect 1 (visibility): the roster names the disagreement ────────────────


def test_roster_flags_a_live_host_that_carries_a_stop_marker() -> None:
    """`online` here is what hid the exclusion: the roster's own source of truth
    (a live probe) contradicted the fan-out's (the marker) with nothing to see."""
    from cli.commands.cluster.control import _status_cell

    stopped = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"
    # the other three verdicts are unchanged
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert _status_cell(online=True, identity_mismatch=True, stopped_at=stopped) == "MISMATCH"


# ─── Defect 3: declined restart vs failed restart ────────────────────────────


def test_declined_restart_reports_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """A preflight refusal stops nothing, so the host is still serving. It must be
    distinguishable from a failure after the stop — the updater shell branches on
    exactly this code to decide whether to run `ava start`."""

    stopped: list[bool] = []
    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda _db: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_k: stopped.append(True) or 0)  # type: ignore[func-returns-value]
    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", lambda: None)

    assert (
        _stop_commands.cmd_restart(
            retained_children=[], database_factory=operator_database, producer=operator_pipeline
        )
        == RESTART_DECLINED_EXIT_CODE
    )
    assert stopped == []  # validate-before-kill: nothing was taken down


def test_failed_restart_after_the_stop_is_not_reported_as_declined(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """Once the stop has happened the host may be DOWN, so its code must NOT be the
    one the updater treats as "still serving"."""
    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda _db: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def start_result(
        operation: object, database_factory: Callable[[], Any], **kwargs: object
    ) -> int:
        assert database_factory is operator_database
        assert kwargs["producer"] is operator_pipeline
        return 1

    monkeypatch.setattr(_start_commands, "_cmd_start_body", start_result)

    rc = _stop_commands.cmd_restart(
        retained_children=[], database_factory=operator_database, producer=operator_pipeline
    )
    assert rc != 0
    assert rc != RESTART_DECLINED_EXIT_CODE


def _paused_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_release_self_heal_pause`'s posture read answer `paused` — the pause
    state it heals (R1 old-signal sweep, PR5: the posture row replaced the
    `cluster_paused` file)."""
    from base.deploy.state.host_deploy_state import HostDeployState

    monkeypatch.setattr(
        "base.deploy.state.host_deploy_state.read",
        lambda _db, *_a, **_k: HostDeployState(  # pyright: ignore[reportUnknownArgumentType]
            machine="test",
            posture="paused",
            updated_at=datetime.now(UTC),
        ),
    )


def test_declined_restart_releases_a_pause_nothing_else_owns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    """A locally spawned self-heal pauses this host before running `ava restart`. If
    that restart declines, nothing else clears the pause — so a healthy host would
    sit with its restarter killed until an operator noticed."""

    _paused_posture(monkeypatch)
    monkeypatch.setattr("base.deploy.maintenance.admission.snapshot", lambda: None)
    unpaused: list[bool] = []
    monkeypatch.setattr(
        "ops.cluster.pause.unpause_local_cluster",
        lambda _db, _bus: unpaused.append(True),  # pyright: ignore[reportUnknownArgumentType]
    )

    _stop_commands._release_self_heal_pause(database_factory=operator_database)
    assert unpaused == [True]


def test_declined_restart_leaves_a_stop_holds_pause_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_database: Callable[[], Any]
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
    monkeypatch.setattr("base.deploy.maintenance.admission.snapshot", lambda: held)
    unpaused: list[bool] = []
    monkeypatch.setattr(
        "ops.cluster.pause.unpause_local_cluster",
        lambda _db, _bus: unpaused.append(True),  # pyright: ignore[reportUnknownArgumentType]
    )

    _stop_commands._release_self_heal_pause(database_factory=operator_database)
    assert unpaused == []
