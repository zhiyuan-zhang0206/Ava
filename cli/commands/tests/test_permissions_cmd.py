from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from base.host.permissions_helper.grants import checklist, gate
from base.host.permissions_helper.grants.catalog import PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult
from cli.commands import permissions as cmd
from services.desktop.permissions_helper.onboarding import GateService

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _report(**overrides: Grant) -> gate.GateReport:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    for item_id, grant in overrides.items():
        for service in gate.item_for(item_id).services:
            grants[service] = grant
    return gate.evaluate(
        ProbeResult(
            grants=grants,
            screen_recording=overrides.get("screen-recording") is not Grant.DENIED,
            ax_trusted=overrides.get("accessibility") is not Grant.DENIED,
        ),
        now=NOW,
    )


def _args(**kwargs: Any) -> argparse.Namespace:
    base = {"permissions_cmd": "status", "json": False, "timeout": 90.0}
    base.update(kwargs)
    return argparse.Namespace(**base)


class FakeService(GateService):
    def __init__(self, report: gate.GateReport, *, restart: str = "") -> None:
        self._report = report
        self._restart = restart
        self.calls: list[tuple[str, dict[str, object]]] = []

    def check(self) -> gate.GateReport:
        self.calls.append(("check", {}))
        return self._report

    def onboard(self, **kwargs: object) -> gate.GateReport:
        self.calls.append(("onboard", kwargs))
        return self._report

    def defer(self) -> None:
        self.calls.append(("defer", {}))

    def restart_helper(self) -> str:
        self.calls.append(("restart_helper", {}))
        return self._restart


def test_status_exits_nonzero_while_something_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeService(_report(**{"screen-recording": Grant.DENIED}))
    assert cmd.run(_args(), service) == 1
    assert "Screen Recording" in capsys.readouterr().out


def test_status_exits_zero_on_a_complete_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run(_args(), FakeService(_report())) == 0
    assert "Every required authorization is granted." in capsys.readouterr().out


def test_json_output_is_the_checklist_payload(capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(**{"desktop-folder": Grant.NOT_DETERMINED})
    cmd.run(_args(json=True), FakeService(report))
    payload = json.loads(capsys.readouterr().out)
    assert payload == checklist.payload(report)
    assert payload["required_missing"] == 1


def test_onboard_refuses_to_prompt_without_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeService(_report())
    assert cmd.run(_args(permissions_cmd="onboard"), service, interactive=False) == 0
    assert service.calls[0][0] == "onboard"
    assert service.calls[0][1]["interactive"] is False


def test_defer_says_plainly_that_nothing_was_granted(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run(_args(permissions_cmd="defer"), FakeService(_report())) == 0
    assert "Nothing is granted by this" in capsys.readouterr().out


def test_restart_helper_prints_the_operator_sentence(capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeService(_report(), restart="restarted the permissions helper")
    assert cmd.run(_args(permissions_cmd="restart-helper"), service) == 0
    assert "restarted" in capsys.readouterr().out
