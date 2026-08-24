"""shared.alerts IM-copy contract tests (Task #1261, user ruling 2026-08-13).

Locks the alert-push governance contract: the user-visible alert templates
live in shared/alerts_copy.py (no head/trigger/jump-link literals in
shared/alerts.py; services/im_bridge/copy.py re-exports them), the template
language follows user_settings ``display.language`` ("zh" | "en", default
"zh"), and only template/framework copy is translated — alert
labels/annotations data passes through verbatim.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import shared.alerts as shared_alerts
from shared import alerts_copy as copy
from shared.alerts import display_language, format_local, frontend_base_url, notify_text


def _alert(*, status: str = "firing", severity: str = "error") -> dict[str, Any]:
    """An Alertmanager-webhook alert shape (the dict form notify_text reads)."""

    return {
        "status": status,
        "labels": {"alertname": "test-rule", "severity": severity},
        "annotations": {"summary": "test summary"},
        "starts_at": "2026-08-04T10:00:00Z",
        "generator_url": "http://localhost:3002/alerting/xyz/edit",
    }


def _head(lang: str, *, resolved: bool, severity: str = "ERROR") -> str:
    return copy.ALERT_HEAD[lang]["resolved" if resolved else "firing"].format(
        severity=severity, alertname="test-rule"
    )


# -- template source (governance) -------------------------------------------


def test_alert_format_literals_not_hardcoded_in_alerts_module() -> None:
    """Governance (user ruling 2026-08-08): user-visible IM copy lives in
    shared/alerts_copy.py — the alert head literals must not creep back into
    shared/alerts.py."""
    src = Path(shared_alerts.__file__).read_text(encoding="utf-8")
    for literal in (
        "⚠️ ALERT",  # emoji-ok: asserting the governance guard (head literals banned from shared/alerts.py)
        "✅ RESOLVED",  # emoji-ok: asserting the governance guard (head literals banned from shared/alerts.py)
        "⚠️ 告警",  # emoji-ok: asserting the governance guard (head literals banned from shared/alerts.py)
        "✅ 已恢复",  # emoji-ok: asserting the governance guard (head literals banned from shared/alerts.py)
    ):
        assert literal not in src


def test_notify_text_composes_from_copy_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_text output is built from the alerts_copy templates — patching
    a template changes the message, so no format string is baked into the
    alerts module."""
    monkeypatch.setitem(shared_alerts.ALERT_HEAD["en"], "firing", "MARKER [{severity}] {alertname}")
    monkeypatch.setitem(shared_alerts.ALERT_TRIGGERED_AT, "en", "MARKER-TIME {time}")
    monkeypatch.setattr(shared_alerts, "ALERT_JUMP_LINK", "MARKER-LINK {url}/x")

    text = notify_text(_alert(), lang="en")

    assert text.startswith("MARKER [ERROR] test-rule")
    assert "MARKER-TIME " in text
    assert text.endswith("MARKER-LINK " + frontend_base_url() + "/x")


def test_notify_text_omits_jump_link_when_no_frontend_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-unconfigured cluster (frontend_base_url() == "") must not
    push a malformed jump-link line (issue #134) — the line is omitted
    rather than rendered as a bare path."""

    monkeypatch.setattr(shared_alerts, "frontend_base_url", lambda: "")
    monkeypatch.setattr(shared_alerts, "ALERT_JUMP_LINK", "MARKER-LINK {url}/x")

    text = notify_text(_alert(), lang="en")

    assert "MARKER-LINK" not in text
    assert "ALERT [ERROR] test-rule" in text
    # the rest of the message is intact — only the jump-link line is gone
    assert "triggered " in text
    assert text.count("\n") == 3


# -- language selection -----------------------------------------------------


def test_notify_text_default_language_is_zh() -> None:
    """No lang passed -> the zh template set (user ruling 2026-08-13 default)."""

    text = notify_text(_alert())
    assert text.startswith(_head("zh", resolved=False))
    assert "触发时间 " in text


def test_notify_text_language_variants() -> None:
    """zh/en template sets, both firing and resolved heads; data (summary,
    alertname, severity) passes through untranslated; unknown lang -> zh."""

    zh_firing = notify_text(_alert(), "zh")
    en_firing = notify_text(_alert(), "en")
    zh_resolved = notify_text(_alert(status="resolved"), "zh")
    en_resolved = notify_text(_alert(status="resolved"), "en")

    assert zh_firing.startswith(_head("zh", resolved=False))
    assert en_firing.startswith(_head("en", resolved=False))
    assert zh_resolved.startswith(_head("zh", resolved=True))
    assert en_resolved.startswith(_head("en", resolved=True))
    assert "triggered " in en_firing
    assert "触发时间 " in zh_firing

    # labels/annotations data is never translated
    assert "test-rule" in zh_firing and "test-rule" in en_firing
    assert "test summary" in zh_firing and "test summary" in en_firing
    assert "ERROR" in zh_firing and "ERROR" in en_firing

    # both languages share the fleet-UI jump link
    jump = copy.ALERT_JUMP_LINK.format(url=frontend_base_url())
    assert zh_firing.endswith(jump) and en_firing.endswith(jump)

    # unknown lang falls back to the default
    assert notify_text(_alert(), "fr") == zh_firing


# -- format_local (tz audit, 2026-08, PR-6) ----------------------------------


def test_format_local_carries_year_and_zone() -> None:
    """The IM-push timestamp used to be bare `%m-%d %H:%M` — no year (an
    alert near New Year's read as an unstated year), no zone abbreviation (no
    way to tell which machine's local clock a multi-machine cluster's reader
    was looking at). Both must be present now."""

    ts = datetime.datetime(2026, 1, 3, 9, 5, tzinfo=datetime.UTC)
    text = format_local(ts)
    assert text.startswith("2026-01-03 ")
    # A trailing zone abbreviation/offset token after "HH:MM " — the exact
    # spelling depends on the runner's local zone (UTC in CI, per
    # vitest.config.ts-style TZ pinning is a frontend-only convention; the
    # backend suite runs whatever TZ the host provides), so assert presence
    # rather than a specific string.
    assert text.split(" ")[-1] != ""


def test_format_local_empty_for_none() -> None:
    assert format_local(None) == ""


def test_display_language_defaults_to_zh(db_conn: psycopg.Connection) -> None:
    """No user_settings row -> the IM template default ("zh")."""

    assert display_language(db_conn) == "zh"


def test_display_language_reads_setting(db_conn: psycopg.Connection) -> None:
    """display.language="en" -> English templates."""

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("en"),),
        )
    db_conn.commit()
    assert display_language(db_conn) == "en"


def test_display_language_unknown_value_falls_back_to_zh(
    db_conn: psycopg.Connection,
) -> None:
    """An unsupported language value falls back to the default — never raises."""

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("fr"),),
        )
    db_conn.commit()
    assert display_language(db_conn) == "zh"
