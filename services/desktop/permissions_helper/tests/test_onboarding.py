from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from base.host.permissions_helper.client import (
    PermissionsHelperClient,
    PermissionsHelperError,
    SpawnResult,
)
from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PREFLIGHT_SERVICES, Grant, PermissionItem
from base.host.permissions_helper.grants.probe import HelperChannel
from services.desktop.permissions_helper import onboarding

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)


class FakeHelper(PermissionsHelperClient):
    def __init__(self, codes: dict[str, int], *, screen: bool = True, ax: bool = True) -> None:
        super().__init__("/unused.sock")
        self._codes = codes
        self._screen = screen
        self._ax = ax
        self.spawns: list[list[str]] = []

    def ping(self) -> Any:
        return {"preflight_screen": self._screen, "ax_trusted": self._ax}

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> SpawnResult:
        self.spawns.append(list(argv))
        services = json.loads(argv[2])
        Path(argv[3]).write_text(
            json.dumps({service: {"code": self._codes[service]} for service in services})
        )
        return {"pid": 1, "reused": False}


class DeadHelper(PermissionsHelperClient):
    def __init__(self) -> None:
        super().__init__("/unused.sock")

    def ping(self) -> Any:
        raise PermissionsHelperError("no socket")


def _granted(_client: HelperChannel, _item: PermissionItem, **_options: object) -> str:
    return "granted"


def _codes(value: int) -> dict[str, int]:
    return dict.fromkeys(PREFLIGHT_SERVICES, value)


def host(
    tmp_path: Path, *, client: FakeHelper | DeadHelper | None = None, platform: str = "darwin"
) -> onboarding.GateHost:
    return onboarding.GateHost(
        client=client if client is not None else FakeHelper(_codes(0)),
        env={},
        workdir=tmp_path / "scratch",
        home=tmp_path,
        record_path=tmp_path / gate.RECORD_NAME,
        platform=platform,
    )


def test_check_probes_and_remembers_what_it_saw(tmp_path: Path) -> None:
    seat = host(tmp_path)
    report = onboarding.check(host=seat, now=NOW)
    assert report.state is gate.GateState.COMPLETE
    stored = gate.read_record(seat.record_path)
    assert stored is not None
    assert stored.state is gate.GateState.COMPLETE
    assert stored.grants["screen-recording"] is Grant.GRANTED


def test_a_deferred_host_stays_deferred_across_checks(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(2)))
    onboarding.defer(now=NOW, host=seat)
    report = onboarding.check(host=seat, now=NOW)
    assert report.state is gate.GateState.DEFERRED
    assert report.restricted


def test_an_unreachable_helper_is_reported_as_unavailable(tmp_path: Path) -> None:
    report = onboarding.check(host=host(tmp_path, client=DeadHelper()), now=NOW)
    assert report.state is gate.GateState.UNAVAILABLE


def test_a_non_darwin_host_has_nothing_to_check(tmp_path: Path) -> None:
    report = onboarding.check(host=host(tmp_path, client=DeadHelper(), platform="linux"), now=NOW)
    assert report.state is gate.GateState.NOT_APPLICABLE
    assert not report.restricted


def test_a_non_interactive_pass_never_raises_a_dialog(tmp_path: Path) -> None:
    asked: list[str] = []
    seat = host(tmp_path, client=FakeHelper(_codes(2)))

    def request_item(*args: object, **kwargs: object) -> str:
        asked.append("called")
        return "granted"

    report = onboarding.onboard(host=seat, interactive=False, now=NOW, request_item=request_item)
    assert asked == []
    assert isinstance(seat.client, FakeHelper)
    assert len(seat.client.spawns) == 1
    assert report.state is gate.GateState.ACTION_REQUIRED


def test_an_interactive_pass_requests_only_what_a_dialog_can_still_resolve(tmp_path: Path) -> None:
    asked: list[str] = []
    seat = host(tmp_path, client=FakeHelper(_codes(2)))

    def request_item(_client: HelperChannel, item: PermissionItem, **_options: object) -> str:
        asked.append(item.id)
        return "granted"

    report = onboarding.onboard(
        host=seat, interactive=True, now=NOW, sleep=lambda _: None, request_item=request_item
    )
    assert asked == [item.id for item in report.prompt_now()]
    assert "desktop-folder" in asked
    assert "screen-recording" not in asked
    assert "automation-finder" not in asked


def test_the_request_outcome_is_not_its_own_evidence(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(1)))
    report = onboarding.onboard(
        host=seat,
        interactive=True,
        now=NOW,
        sleep=lambda _: None,
        request_item=_granted,
    )
    assert report.state is gate.GateState.ACTION_REQUIRED
    assert report.grant_for("desktop-folder") is Grant.DENIED


def test_the_summary_names_what_is_missing(tmp_path: Path) -> None:
    report = onboarding.check(
        host=host(tmp_path, client=FakeHelper(_codes(1), screen=False, ax=False)), now=NOW
    )
    summary = onboarding.missing_summary(report)
    assert summary.startswith("5 permission(s) still missing")
    assert "Screen Recording" in summary
    assert onboarding.observations(report)["accessibility"] == "denied"
