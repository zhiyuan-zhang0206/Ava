"""shared.alerts IM-copy contract tests (Task #1261, user ruling 2026-08-13).

Locks the alert-push governance contract: the user-visible alert templates
live in services/im_bridge/copy.py (no head/trigger/jump-link literals in
shared/alerts.py), the template language follows user_settings
``display.language`` ("zh" | "en", default "zh"), and only template/framework
copy is translated — alert labels/annotations data passes through verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import shared.alerts as shared_alerts
from services.im_bridge import copy
from shared.alerts import display_language, frontend_base_url, notify_text


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
    services/im_bridge/copy.py — the alert head literals must not creep back
    into shared/alerts.py."""
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
    """notify_text output is built from the copy.py templates — patching a
    template changes the message, so no format string is baked into the
    alerts module."""
    monkeypatch.setitem(copy.ALERT_HEAD["en"], "firing", "MARKER [{severity}] {alertname}")
    monkeypatch.setitem(copy.ALERT_TRIGGERED_AT, "en", "MARKER-TIME {time}")
    monkeypatch.setattr(copy, "ALERT_JUMP_LINK", "MARKER-LINK {url}/x")

    text = notify_text(_alert(), lang="en")

    assert text.startswith("MARKER [ERROR] test-rule")
    assert "MARKER-TIME " in text
    assert text.endswith("MARKER-LINK " + frontend_base_url() + "/x")


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
