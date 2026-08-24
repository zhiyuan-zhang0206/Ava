"""Alert-push IM copy — the user-visible alert templates (single source).

The alert push templates were moved here from ``services/im_bridge/copy.py``
(2026-08-25, tech-audit P1): ``shared.alerts`` — the alert-ingest core — must
not import up into ``services``, and the templates are its own IM copy, so
they live in a ``shared`` leaf module and ``services/im_bridge/copy.py``
re-exports them for its own consumers. The "one voice for IM copy" governance
(ruling 2026-08-08) is preserved: one definition, re-exported, never
duplicated.

Language follows the UI language — user_settings ``display.language``
("zh" | "en"; missing/unknown falls back to ALERT_LANGUAGE_DEFAULT, user
ruling 2026-08-13). Only template/framework copy is translated: alert
labels/annotations data (severity, alertname, summary, generator_url) passes
through untranslated. The English set keeps the pre-ruling production
strings; {severity}/{alertname}/{time}/{url} are filled in at the call site.
"""

from __future__ import annotations

ALERT_LANGUAGES = ("zh", "en")
ALERT_LANGUAGE_DEFAULT = "zh"

ALERT_HEAD = {
    "zh": {
        "firing": "⚠️ 告警 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ 已恢复 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
    "en": {
        "firing": "⚠️ ALERT [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ RESOLVED [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
}
ALERT_TRIGGERED_AT = {
    "zh": "触发时间 {time}",
    "en": "triggered {time}",
}
ALERT_JUMP_LINK = "→ {url}/insights/alerts"
