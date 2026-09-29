"""An ordinary start never releases the cutover hold; only the cutover flow does.

The adoption journal records the hold it adopted or created. While exactly
that hold stands, `ava start` (typed by the operator, or run by the autostart
job after a reboot) starts held and leaves admission closed until the go/no-go
step `cutover_adopt_home.py --resume`. Any other hold keeps the ordinary behavior.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from base.config import settings
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission
from base.deploy.maintenance.state import MaintenanceHold
from cli.commands.lifecycle._pause_resume import resume_after_start
from scripts import cutover_adopt_home as adopt
from tests.lifecycle.cutover.conftest import SERVICE_PATH, LegacyHome

Make = Callable[..., LegacyHome]


def _adopted(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> tuple[LegacyHome, str, datetime]:
    legacy = make_legacy(roles=("gateway", "agent-runner"))
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry)]
    argv += ["--service-path", SERVICE_PATH, "--execute", "--cutover-id", "c9"]
    argv += ["--expect-mode", "gateway"]
    assert adopt.main(argv, host=legacy.scheduler.host(), checkout=legacy.checkout) == 0
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    hold = json.loads((legacy.home / "cutover-rollback" / "adopt-home.json").read_text())["hold"]
    return legacy, hold["holder"], datetime.fromisoformat(hold["acquired_at"])


def _bare_start(monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[], int], MagicMock, list[bool]]:
    """An ordinary `ava start` body behind the real release boundary."""
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    authorized: list[bool] = []

    @resume_after_start
    def start() -> int:
        authorized.append(admission.start_authorized())
        return 0

    return start, unpause, authorized


def test_a_start_after_the_held_first_start_keeps_the_cutover_hold(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reboot's autostart (or a typed `ava start`) between the held first start
    and the go/no-go gate brings the unit up and leaves business closed."""
    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    admission.set_phase(holder, at, "starting")
    admission.set_phase(holder, at, "ready")
    start, unpause, authorized = _bare_start(monkeypatch)
    capsys.readouterr()

    assert start() == 0

    assert authorized == [True]
    unpause.assert_not_called()
    current = admission.require_operation(holder, at)
    assert current.maintenance is not None and current.maintenance.phase == "ready"
    assert admission.business_paused()
    out = capsys.readouterr().out
    assert f"cutover_adopt_home.py --home {legacy.home} --resume" in out
    assert "hold released" not in out


@pytest.mark.parametrize("serving", [True, False])
def test_a_start_that_passes_readiness_completes_a_starting_cutover_hold(
    make_legacy: Make,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    serving: bool,
) -> None:
    """A failed or unready held first start leaves phase `starting`. The next start
    that passes readiness inside the hold (the autostart after a reboot) completes
    it to `ready`, so the release command it prints is accepted; a start that is
    not serving leaves `starting` and prints no release command."""
    _legacy, holder, at = _adopted(make_legacy, monkeypatch)
    admission.set_phase(holder, at, "starting")
    start, unpause, authorized = _bare_start(monkeypatch)
    monkeypatch.setattr(start_serving, "is_serving", lambda: serving)
    capsys.readouterr()

    assert start() == 0

    assert authorized == [True]
    unpause.assert_not_called()
    current = admission.require_operation(holder, at)
    assert current.maintenance is not None
    assert current.maintenance.phase == ("ready" if serving else "starting")
    assert admission.business_paused()
    assert ("--resume" in capsys.readouterr().out) == serving


def test_a_start_before_the_held_first_start_refuses_and_names_it(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At W6 the gateway still owes the database-records repair (W7): an ordinary
    start must not bring it up, only point at the cutover's own held start."""
    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    start, unpause, authorized = _bare_start(monkeypatch)

    with pytest.raises(RuntimeError, match=r"cutover_adopt_home\.py --home .* --start") as refused:
        start()

    assert str(legacy.home) in str(refused.value)
    assert authorized == []
    unpause.assert_not_called()
    current = admission.require_operation(holder, at)
    assert current.maintenance is not None and current.maintenance.phase == "stopped"


def test_any_other_hold_is_still_released_by_an_ordinary_start(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the go/no-go resume, the next ordinary stop's hold is no cutover hold."""
    legacy, _holder, _at = _adopted(make_legacy, monkeypatch)
    later = {
        "state": "paused",
        "holder": "local-stop:legacy-box:9",
        "acquired_at": "2026-10-01T00:00:00+00:00",
        "maintenance": MaintenanceHold("stopped").encode(),
        "driver": None,
    }
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(later))
    start, unpause, authorized = _bare_start(monkeypatch)

    assert start() == 0

    assert authorized == [True]
    unpause.assert_called_once_with()


_LATER = ("local-stop:legacy-box:9", datetime.fromisoformat("2026-10-01T00:00:00+00:00"))


@pytest.mark.parametrize(
    ("case", "refused"),
    [
        ("cutover-stopped", True),
        ("cutover-unreadable-journal", True),
        ("cutover-starting", False),
        ("later-unreadable-journal", False),
    ],
)
def test_maintenance_start_is_no_side_door_around_the_held_first_start(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, case: str, refused: bool
) -> None:
    """`ava maintenance start` with the cutover hold's exact generation refuses
    in phase `stopped`, as an ordinary start does: at W6 a gateway still owes
    the records repair (W7), which the held first start waits for. A damaged
    journal keeps a `cutover:` hold refused; a later hold still starts, the
    documented way out of such a journal. From `starting` on it starts held."""
    from cli.commands.lifecycle import maintenance as maintenance_command

    legacy, holder, at = _adopted(make_legacy, monkeypatch)
    if case == "cutover-starting":
        admission.set_phase(holder, at, "starting")
    if case == "later-unreadable-journal":
        holder, at = _LATER
        owner = {
            "state": "paused",
            "holder": holder,
            "acquired_at": at.isoformat(),
            "maintenance": MaintenanceHold("stopped").encode(),
        }
        (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(owner))
    if case.endswith("unreadable-journal"):
        journal = legacy.home / "cutover-rollback" / "adopt-home.json"
        journal.write_text(journal.read_text()[:40])
    starts: list[bool] = []

    def cmd_start(*, persist_services: bool) -> int:
        starts.append(admission.start_authorized())
        return 0

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", cmd_start)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)

    if refused:
        with pytest.raises(RuntimeError, match=r"cutover_adopt_home\.py --home .* --start") as exc:
            maintenance_command._start(holder, at)
        assert str(legacy.home) in str(exc.value)
    else:
        assert maintenance_command._start(holder, at) == 0

    held = admission.require_operation(holder, at).maintenance
    assert held is not None and held.phase == ("stopped" if refused else "ready")
    assert starts == ([] if refused else [True])
    assert admission.business_paused()


def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, adopted: bool) -> Path:
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    if adopted:
        hold = {"holder": "cutover:c1", "acquired_at": "2026-09-28T00:00:00+00:00"}
        journal = home / "cutover-rollback" / "adopt-home.json"
        journal.parent.mkdir()
        journal.write_text(json.dumps({"home": str(home), "hold": {**hold, "origin": "cutover"}}))
        owner = {"state": "paused", **hold, "maintenance": MaintenanceHold("stopped").encode()}
        (home / "run" / "deploy-pause-owner.json").write_text(json.dumps(owner))
    return home


@pytest.mark.parametrize("adopted", [True, False])
def test_the_ledger_refusal_names_the_start_that_fits_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    from cli.commands.data_plane.bringup import cutover_instruction

    home = _journal_home(tmp_path, monkeypatch, adopted=adopted)
    text = cutover_instruction(home)
    held = f"scripts/cutover_adopt_home.py --home {home} --start"
    assert (held in text, "then `ava start`" in text) == (adopted, not adopted)


@pytest.mark.parametrize("adopted", [True, False])
def test_the_data_plane_cutover_names_the_start_that_fits_the_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adopted: bool,
) -> None:
    from scripts import cutover_db_authority as cutover

    home = _journal_home(tmp_path, monkeypatch, adopted=adopted)
    monkeypatch.setattr(cutover, "_run", lambda *_args, **_kwargs: None)

    assert cutover.main(["--home", str(home), "--execute"]) == 0

    out = capsys.readouterr().out
    held = f"scripts/cutover_adopt_home.py --home {home} --start"
    assert (held in out, "run `ava start`" in out) == (adopted, not adopted)
