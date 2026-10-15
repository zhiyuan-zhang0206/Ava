from __future__ import annotations

from typing import Any

import pytest

from base.db.code_version_gate import ProcessDbGate
from base.host.permissions_helper.client import PermissionsHelperError

from ..test_computer_mcp_daemon import (
    FakeHelper,
    call,
    computer_daemon,
)
from ..test_computer_mcp_daemon import (
    audit_log as audit_log,
)
from ..test_computer_mcp_daemon import (
    fake_helper as fake_helper,
)


def _stops(fake_helper: FakeHelper) -> int:
    return [call[0] for call in fake_helper.calls].count("indicator_stop")


async def test_a_successful_release_ends_the_badge(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = computer_daemon(database_gate=database_gate, helper=fake_helper)
    assert (await call(d, "type_text", {"text": "hi"}))["ok"] is True
    assert _stops(fake_helper) == 0, "an action alone must not talk to the badge"
    released = await call(d, "release_control")
    assert released["ok"] is True
    assert _stops(fake_helper) == 1


async def test_a_refused_release_keeps_the_badge(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = computer_daemon(database_gate=database_gate, helper=fake_helper)
    assert (await call(d, "type_text", {"text": "hi"}))["ok"] is True
    refused = await call(d, "release_control", agent_id=8)
    assert refused["ok"] is False
    assert _stops(fake_helper) == 0


@pytest.mark.parametrize(
    "failure",
    [
        PermissionsHelperError("permissions helper lacks capture_indicator_v1"),
        PermissionsHelperError("helper unreachable"),
        OSError("socket closed"),
    ],
)
async def test_a_failed_stop_is_logged_and_the_release_still_succeeds(
    fake_helper: FakeHelper,
    audit_log: list,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    *,
    database_gate: ProcessDbGate,
) -> None:
    fake_helper.indicator_stop_error = failure
    d = computer_daemon(database_gate=database_gate, helper=fake_helper)
    assert (await call(d, "type_text", {"text": "hi"}))["ok"] is True
    released = await call(d, "release_control")
    assert released["ok"] is True
    [warning] = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert "capture indicator" in warning["message"] and str(failure) in warning["message"]
    [row] = [
        ev
        for ev in audit_log
        if ev["event_type"] == "computer_action" and ev["payload"]["action"] == "release_control"
    ]
    assert row["payload"]["outcome"] == "ok"
