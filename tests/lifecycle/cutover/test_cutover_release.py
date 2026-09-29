"""The cutover hold has one exit: the go/no-go step `cutover_adopt_home.py --resume`.

Every other release door refuses it and names that step: `ava cluster recover`
and `ava maintenance resume` here, the ordinary start in
`test_cutover_hold.py`. The step itself refuses until the cutover's own
records allow it: a complete adoption, a held first start that reached
`ready` and, on a gateway, a completed database-records repair (W7).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cli.commands import maintenance as maintenance_command
from ops import cluster_pause
from scripts import cutover_adopt_home as adopt
from scripts import cutover_db_records as records
from shared import maintenance, pause_owner, start_serving
from shared.config import settings
from shared.maintenance_state import MaintenanceHold
from tests.lifecycle.cutover.conftest import SERVICE_PATH, LegacyHome, record_repair
from tests.lifecycle.cutover.test_cutover_hold import _adopted, _bare_start

Make = Callable[..., LegacyHome]
_UNPAUSE = cluster_pause.unpause_local_cluster


def _ready(holder: str, at: datetime) -> None:
    maintenance.set_phase(holder, at, "starting")
    maintenance.set_phase(holder, at, "ready")


def _resume_args(holder: str, at: datetime) -> argparse.Namespace:
    return argparse.Namespace(
        maintenance_cmd="resume", operation=holder, acquired_at=at.isoformat(), cancel=False
    )


def _release_seams(monkeypatch: pytest.MonkeyPatch, roles: frozenset[str]) -> MagicMock:
    """The real `maintenance resume` body, minus its database and wake effects."""
    monkeypatch.setattr(cluster_pause, "unpause_local_cluster", _UNPAUSE)
    monkeypatch.setattr(maintenance_command, "machine_role", lambda: roles)
    monkeypatch.setattr(maintenance_command, "host_identity_or_none", lambda: None)
    monkeypatch.setattr(maintenance_command, "connect", MagicMock())
    monkeypatch.setattr("shared.host_deploy_state.set_posture", MagicMock())
    monkeypatch.setattr("ops.cluster_pause._settle_stranded_reaps", MagicMock())
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    wake = MagicMock()
    monkeypatch.setattr("ops.agent_pause._wake", wake)
    return wake


def test_recover_refuses_while_the_cutover_hold_stands(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Between the held first start and the go/no-go gate the unit looks like a
    stranded pause; recover must not be a second exit that skips the gate."""
    from cli.commands.cluster import recover

    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    _ready(holder, at)
    op = MagicMock(return_value={"unlocked_holder": None})
    monkeypatch.setattr("ops.cluster.cluster_recover_op", op)
    monkeypatch.setattr("shared.cluster_lock.update_lock_holder", lambda: None)

    assert recover.cmd_cluster_recover() == 1

    op.assert_not_called()
    assert maintenance.business_paused()
    err = capsys.readouterr().err
    assert f"cutover_adopt_home.py --home {legacy.home} --resume" in err


def test_maintenance_resume_refuses_the_cutover_hold_and_names_the_gate(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    _ready(holder, at)
    wake = _release_seams(monkeypatch, frozenset({"gateway", "agent-runner"}))

    with pytest.raises(RuntimeError, match=r"cutover_adopt_home\.py --home .* --resume") as refused:
        maintenance_command.run(_resume_args(holder, at))

    assert str(legacy.home) in str(refused.value)
    wake.assert_not_called()
    assert maintenance.business_paused()


@pytest.mark.parametrize("later_holder", ["local-pause:legacy-box:77:later", "fleet:op-later"])
def test_an_unreadable_journal_keeps_the_exact_holder_resume_as_the_way_out(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, later_holder: str
) -> None:
    """A truncated adoption journal fails the ordinary start closed; the
    exact-holder resume (after `ava maintenance start`) still releases a later
    hold. The journal belongs to the cutover alone, so a hold another subsystem
    took (`fleet:`, `pitr:`, `recovery:`) resumes as it would without one."""
    legacy, _holder, _at = _adopted(make_legacy, monkeypatch)
    later = (later_holder, datetime.fromisoformat("2026-10-01T00:00:00+00:00"))
    owner = {
        "state": "paused",
        "holder": later[0],
        "acquired_at": later[1].isoformat(),
        "maintenance": MaintenanceHold("ready").encode(),
    }
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(owner))
    journal = legacy.home / "cutover-rollback" / "adopt-home.json"
    journal.write_text(journal.read_text()[:40])
    start, unpause, _authorized = _bare_start(monkeypatch)
    with pytest.raises(RuntimeError, match="unreadable adoption journal"):
        start()
    unpause.assert_not_called()
    _release_seams(monkeypatch, frozenset({"agent-runner"}))

    assert maintenance_command.run(_resume_args(*later)) == 0

    assert pause_owner.read().status == "resumed"


def test_an_unreadable_journal_never_resumes_a_cutover_holder(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a created cutover hold is named `cutover:<id>`, and no later hold
    takes that prefix: damage to the journal re-opens no exact-holder resume
    that would skip the go/no-go step's checks."""
    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    assert holder.startswith("cutover:")
    _ready(holder, at)
    journal = legacy.home / "cutover-rollback" / "adopt-home.json"
    journal.write_text(journal.read_text()[:40])
    wake = _release_seams(monkeypatch, frozenset({"gateway", "agent-runner"}))

    with pytest.raises(RuntimeError, match="unreadable adoption journal") as refused:
        maintenance_command.run(_resume_args(holder, at))

    assert f"cutover_adopt_home.py --home {legacy.home} --resume" in str(refused.value)
    wake.assert_not_called()
    assert maintenance.business_paused()
    assert pause_owner.read().matches(holder, at)


def _release(legacy: LegacyHome) -> int:
    return adopt.main(["--home", str(legacy.home), "--resume"], checkout=legacy.checkout)


def test_the_go_no_go_step_releases_a_ready_gateway_after_its_records_repair(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    wake = _release_seams(monkeypatch, frozenset({"gateway", "agent-runner"}))
    assert _release(legacy) == 1  # before the held first start
    assert "has not passed readiness" in capsys.readouterr().err
    _ready(holder, at)
    assert _release(legacy) == 1
    assert "database-records repair (W7) recorded no run" in capsys.readouterr().err
    record_repair(legacy.home, "started")
    assert _release(legacy) == 1
    assert "run is incomplete" in capsys.readouterr().err
    assert maintenance.business_paused()
    wake.assert_not_called()
    (legacy.home / records.JOURNAL).unlink()
    record_repair(legacy.home, "done")

    assert _release(legacy) == 0

    current = pause_owner.read()
    assert current.status == "resumed" and current.matches(holder, at)
    assert not maintenance.business_paused()
    wake.assert_called_once()
    assert _release(legacy) == 0  # already released: nothing more happens
    wake.assert_called_once()
    assert "already released" in capsys.readouterr().out


def test_a_remote_unit_needs_no_records_repair_of_its_own(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry)]
    argv += ["--service-path", SERVICE_PATH, "--execute", "--expect-mode", "remote-unit"]
    assert adopt.main(argv, host=legacy.scheduler.host(), checkout=legacy.checkout) == 0
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    hold = json.loads((legacy.home / "cutover-rollback" / "adopt-home.json").read_text())["hold"]
    holder, at = hold["holder"], datetime.fromisoformat(hold["acquired_at"])
    _ready(holder, at)
    _release_seams(monkeypatch, frozenset({"agent-runner"}))

    assert _release(legacy) == 0

    assert pause_owner.read().status == "resumed"


def test_a_gateways_held_first_start_waits_for_its_records_repair(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The held start writes the host's `paused` posture, and the repair's
    first run sets every `paused` posture idle (W7 before W8). Until that run
    completed the start refuses and changes nothing. A runner's held start has
    no such gate (`test_a_remote_units_held_start_installs_its_capability_bundle`):
    it joins through the gateway's bootstrap, served only after this start."""
    import cli.start_intent

    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    starts: list[object] = []

    def started(args: object) -> int:
        starts.append(args)
        return 0

    monkeypatch.setattr(cli.start_intent, "run_start", started)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    argv = ["--home", str(legacy.home), "--start"]
    assert adopt.main(argv, checkout=legacy.checkout) == 1
    assert "(W7) recorded no run" in capsys.readouterr().err
    record_repair(legacy.home, "started")
    assert adopt.main(argv, checkout=legacy.checkout) == 1
    assert "run is incomplete" in capsys.readouterr().err
    held = maintenance.require_operation(holder, at).maintenance
    assert starts == [] and held is not None and held.phase == "stopped"
    (legacy.home / records.JOURNAL).unlink()
    record_repair(legacy.home, "done")

    assert adopt.main(argv, checkout=legacy.checkout) == 0

    held = maintenance.require_operation(holder, at).maintenance
    assert len(starts) == 1 and held is not None and held.phase == "ready"
    assert maintenance.business_paused()
