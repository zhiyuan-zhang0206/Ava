"""`ava cluster release exclude` — a recorded operator decision in the fleet journal.

The real journal and home lock: an included unit leaves only a held
operation, a failed or unknown one at any time, never while a coordinator
holds the lock, and never back into the operation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base import paths as base_paths
from cli.release_operator import exclude as exclude_module
from cli.release_operator.exclude import exclude_unit
from cli.release_transition.journal import create, exclusive, read_operation
from tests.lifecycle.release_fleet.remote import (
    Exchange,
    Gateway,
    Unit,
    UnitEffects,
    coordinate,
    fleet_request,
    run,
    units,
)
from tests.lifecycle.transition.phases import advance_to


@pytest.fixture
def operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    request = fleet_request(tmp_path.resolve())
    create(request)
    monkeypatch.setattr(base_paths, "ava_home", lambda: Path(request.home))
    return request.path, request.units[0].unit.label


def _cmd(path: Path, unit: str, reason: str = "lid closed") -> int:
    return exclude_module.cmd_release_exclude(operation=path.parent.name, unit=unit, reason=reason)


def test_an_included_unit_leaves_only_a_held_operation(
    operation: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path, unit = operation
    assert _cmd(path, unit) == 2
    assert "held for the operator" in capsys.readouterr().err
    with exclusive(path) as journal:
        advance_to(journal, "starting")
        journal.fail("held: the gateway did not start")
    assert _cmd(path, unit) == 0
    assert json.loads(capsys.readouterr().out)["inclusion"] == "excluded"
    fleet = read_operation(path).fleet
    assert fleet is not None
    assert (fleet.units[0].inclusion, fleet.units[0].reason) == ("excluded", "operator: lid closed")
    assert _cmd(path, unit) == 0  # again: nothing changes


def test_a_unit_the_coordinator_marked_unknown_is_excluded_at_any_time(
    operation: tuple[Path, str],
) -> None:
    path, unit = operation
    with exclusive(path) as journal:
        fleet = journal.operation.fleet
        assert fleet is not None
        unknown = fleet.units[0].model_copy(update={"inclusion": "unknown", "reason": "silent"})
        journal.record_fleet(fleet.model_copy(update={"units": (unknown,)}))
    assert _cmd(path, unit) == 0
    fleet = read_operation(path).fleet
    assert fleet is not None and fleet.units[0].inclusion == "excluded"


def test_a_stranger_a_blank_reason_or_a_running_coordinator_is_refused(
    operation: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path, unit = operation
    assert _cmd(path, "win:C:\\Users\\zzy\\.ava") == 2
    assert "no part in this operation" in capsys.readouterr().err
    assert _cmd(path, unit, reason="  ") == 2
    assert "records its reason" in capsys.readouterr().err
    with exclusive(path) as journal:  # a coordinator holds the home lock for its whole run
        journal.fail("held")
        assert _cmd(path, unit) == 2
    assert "lock" in capsys.readouterr().err.lower()
    fleet = read_operation(path).fleet
    assert fleet is not None and fleet.units[0].inclusion == "included"


def test_a_completed_operation_excludes_nothing(operation: tuple[Path, str]) -> None:
    path, _unit = operation
    with exclusive(path) as journal:
        advance_to(journal, "complete")
        fleet = journal.operation.fleet
        assert fleet is not None
        with pytest.raises(ValueError, match="rejoins through a converge"):
            exclude_unit(journal, fleet.units[0].unit, "late")


def test_an_operator_excluded_unit_closes_and_the_held_release_continues(
    tmp_path: Path,
) -> None:
    """At a hold the operator excludes the unit; the continuation orders it to
    close and stay closed, never waits for it again, and commits degraded."""
    request = fleet_request(tmp_path.resolve())
    effects = UnitEffects()
    unit = Unit(Path(request.home), effects, Exchange())
    gateway = Gateway(request, fail="resuming")
    with pytest.raises(RuntimeError, match="injected gateway failure at resuming"):
        run(request, gateway, unit)
    held = read_operation(request.path)
    assert (held.phase, held.terminal) == ("resuming", False) and held.error is not None
    with exclusive(request.path) as journal:
        exclude_unit(journal, request.units[0].unit, "the runner lost power")
        coordinate(journal, gateway, units(request, unit))
    final = read_operation(request.path)
    assert final.fleet is not None and final.fleet.outcome == "degraded"
    status = final.fleet.units[0]
    assert status.inclusion == "excluded"
    assert status.instruction is not None and status.instruction.action == "excluded"
    # Nothing waits for its answer; the unit closes and holds until a converge.
    held_unit = unit.join()
    assert held_unit is not None and not held_unit.terminal
    assert held_unit.unit is not None and held_unit.unit.report is not None
    assert held_unit.unit.report.state == "closed"
    assert effects.events[-1] == ("stopping", "candidate")
    assert [c.stale_units for c in gateway.published] == [(request.units[0].unit,)]
