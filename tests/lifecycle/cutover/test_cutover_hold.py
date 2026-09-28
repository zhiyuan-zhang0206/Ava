"""An ordinary start never releases the cutover hold; only the cutover flow does.

The adoption journal records the hold it adopted or created. While exactly
that hold stands, `ava start` (typed by the operator, or run by the autostart
job after a reboot) starts held and leaves admission closed until the go/no-go
gate's `ava maintenance resume`. Any other hold keeps the ordinary behavior.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.commands._pause_resume import resume_after_start
from scripts import cutover_adopt_home as adopt
from shared import maintenance, start_serving
from shared.config import settings
from shared.maintenance_state import MaintenanceHold
from tests.lifecycle.cutover.conftest import SERVICE_PATH, LegacyHome

Make = Callable[..., LegacyHome]


def _adopted(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> tuple[LegacyHome, str, datetime]:
    legacy = make_legacy(roles=("gateway", "agent-runner"))
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry)]
    argv += ["--service-path", SERVICE_PATH, "--execute", "--cutover-id", "c9"]
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
        authorized.append(maintenance.start_authorized())
        return 0

    return start, unpause, authorized


def test_a_start_after_the_held_first_start_keeps_the_cutover_hold(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reboot's autostart (or a typed `ava start`) between the held first start
    and the go/no-go gate brings the unit up and leaves business closed."""
    _legacy, holder, at = _adopted(make_legacy, monkeypatch)
    maintenance.set_phase(holder, at, "starting")
    maintenance.set_phase(holder, at, "ready")
    start, unpause, authorized = _bare_start(monkeypatch)
    capsys.readouterr()

    assert start() == 0

    assert authorized == [True]
    unpause.assert_not_called()
    current = maintenance.require_operation(holder, at)
    assert current.maintenance is not None and current.maintenance.phase == "ready"
    assert maintenance.business_paused()
    out = capsys.readouterr().out
    assert f"ava maintenance resume --operation {holder} --acquired-at" in out
    assert "hold released" not in out


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
    current = maintenance.require_operation(holder, at)
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
