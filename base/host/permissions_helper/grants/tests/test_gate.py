from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PING_KEYS, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


def _result(**overrides: Grant) -> ProbeResult:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    booleans = dict.fromkeys(PING_KEYS, True)
    for item_id, grant in overrides.items():
        item = gate.item_for(item_id)
        if item.ping_key:
            booleans[item.ping_key] = grant is Grant.GRANTED
        for service in item.services:
            grants[service] = grant
    return ProbeResult(grants=grants, booleans=booleans, supports_request=True)


def test_a_fully_granted_host_is_complete() -> None:
    report = gate.evaluate(_result(), now=NOW)
    assert report.state is gate.GateState.COMPLETE
    assert report.complete and not report.restricted
    assert report.missing == ()


def test_a_denied_item_blocks_completion_and_names_itself() -> None:
    report = gate.evaluate(_result(**{"screen-recording": Grant.DENIED}), now=NOW)
    assert report.state is gate.GateState.ACTION_REQUIRED
    assert report.restricted
    assert [status.item.id for status in report.missing] == ["screen-recording"]
    assert [item.id for item in report.manual()] == ["screen-recording"]


def test_undetermined_prompt_rows_are_what_the_first_pass_requests() -> None:
    report = gate.evaluate(
        _result(**{"desktop-folder": Grant.NOT_DETERMINED, "accessibility": Grant.DENIED}), now=NOW
    )
    assert [item.id for item in report.prompt_now()] == ["desktop-folder"]
    assert "accessibility" in [item.id for item in report.manual()]


def test_an_automation_row_does_not_block_before_it_is_exercised() -> None:
    report = gate.evaluate(_result(), now=NOW)
    assert report.grant_for("automation-finder") is Grant.UNKNOWN
    assert report.complete


def test_report_only_rows_never_block() -> None:
    report = gate.evaluate(
        _result(**{"full-disk-access": Grant.DENIED, "app-data": Grant.NOT_DETERMINED}), now=NOW
    )
    assert report.complete
    assert all(not status.item.required for status in report.missing)


def test_a_deferred_host_is_not_complete_and_says_so() -> None:
    deferred = gate.evaluate(_result(accessibility=Grant.DENIED), deferred=True, now=NOW)
    assert deferred.state is gate.GateState.DEFERRED
    assert deferred.restricted and not deferred.complete
    fresh = gate.evaluate(_result(), deferred=True, now=NOW)
    assert fresh.state is gate.GateState.COMPLETE


def test_an_unreachable_helper_reads_as_unavailable_never_as_missing() -> None:
    report = gate.evaluate(
        ProbeResult(grants={}, helper_reachable=False, diagnostic="socket refused"), now=NOW
    )
    assert report.state is gate.GateState.UNAVAILABLE
    assert report.restricted
    assert {status.grant for status in report.items} == {Grant.UNKNOWN}


def test_the_record_round_trips_and_a_complete_run_clears_deferral(tmp_path: Path) -> None:
    path = tmp_path / gate.RECORD_NAME
    deferred = gate.evaluate(_result(accessibility=Grant.DENIED), deferred=True, now=NOW)
    gate.remember(deferred, deferred=True, path=path)
    stored = gate.read_record(path)
    assert stored is not None and stored.deferred and stored.state is gate.GateState.DEFERRED
    assert stored.grants["accessibility"] is Grant.DENIED

    complete = gate.evaluate(_result(), deferred=True, now=NOW)
    gate.remember(complete, deferred=True, path=path)
    reread = gate.read_record(path)
    assert reread is not None and not reread.deferred and reread.state is gate.GateState.COMPLETE


def test_a_corrupt_record_is_absent_rather_than_a_fact(tmp_path: Path) -> None:
    path = tmp_path / gate.RECORD_NAME
    path.write_text("{not json")
    assert gate.read_record(path) is None
    path.write_text(
        '{"version": 99, "checked_at": "x", "state": "complete", "deferred": false, "grants": {}}'
    )
    assert gate.read_record(path) is None


def test_deferring_records_later_without_inventing_a_grant(tmp_path: Path) -> None:
    path = tmp_path / gate.RECORD_NAME
    gate.mark_deferred(now=NOW, path=path)
    stored = gate.read_record(path)
    assert stored is not None
    assert stored.deferred and stored.state is gate.GateState.DEFERRED
    assert stored.grants == {}


def test_a_grant_the_running_helper_has_not_read_is_stale_not_denied() -> None:
    result = ProbeResult(
        grants=dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED),
        booleans={"preflight_screen": False, "ax_trusted": False},
        supports_request=True,
    )
    report = gate.evaluate(result, now=NOW)
    assert report.grant_for("screen-recording") is Grant.STALE
    assert report.state is gate.GateState.ACTION_REQUIRED
    assert report.prompt_now() == ()
    assert report.requestable() == ()
    assert [status.item.id for status in report.missing] == ["screen-recording", "accessibility"]


def test_a_tri_state_row_keeps_its_denial_without_a_helper_boolean() -> None:
    report = gate.evaluate(_result(**{"desktop-folder": Grant.DENIED}), now=NOW)
    assert report.grant_for("desktop-folder") is Grant.DENIED
    assert report.prompt_now() == ()


def test_the_manual_pass_reaches_a_denied_grant() -> None:
    report = gate.evaluate(_result(accessibility=Grant.DENIED), now=NOW)
    assert [item.id for item in report.requestable()] == ["accessibility"]


def test_a_report_never_claims_the_request_channel_by_default() -> None:
    report = gate.GateReport(state=gate.GateState.COMPLETE, checked_at=NOW, items=())
    assert report.supports_request is False
