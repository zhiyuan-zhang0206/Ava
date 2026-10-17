from __future__ import annotations

from datetime import UTC, datetime

from base.host.permissions_helper.grants import checklist, gate
from base.host.permissions_helper.grants.catalog import CATALOG, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


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


def test_every_catalog_row_appears_with_why_jump_and_verification() -> None:
    rows = checklist.rows(_report())
    assert len(rows) == len(CATALOG)
    for row in rows:
        assert row.why and row.verify
        assert row.jump.startswith("x-apple.systempreferences:")
        assert row.id in {item.id for item in CATALOG}


def test_a_missing_row_carries_the_restart_note_where_macos_needs_one() -> None:
    rows = {row.id: row for row in checklist.rows(_report(**{"screen-recording": Grant.DENIED}))}
    assert "macOS only re-reads this grant at process start" in rows["screen-recording"].restart
    assert rows["accessibility"].restart == ""


def test_the_text_checklist_names_the_missing_item_and_the_recheck_verb() -> None:
    text = checklist.render_text(_report(**{"desktop-folder": Grant.DENIED}))
    assert "Desktop folder" in text
    assert "1 of 10 required authorizations are not granted." in text
    assert "meiva permissions status" in text
    assert "x-apple.systempreferences" in text


def test_a_complete_host_says_so_and_lists_no_actions() -> None:
    text = checklist.render_text(_report())
    assert "Every required authorization is granted." in text
    assert "Re-check with" not in text


def test_the_payload_counts_only_required_rows() -> None:
    payload = checklist.payload(_report(**{"documents-folder": Grant.DENIED}))
    assert payload["state"] == "action_required"
    assert payload["required_total"] == 10
    assert payload["required_missing"] == 1
    assert len(payload["items"]) == len(CATALOG)
    assert payload["headline"] == checklist.headline(_report(**{"documents-folder": Grant.DENIED}))
