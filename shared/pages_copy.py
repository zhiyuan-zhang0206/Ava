"""Page-expired page copy — user-visible page copy (locale module).

The page-expired response the gateway serves when a page TTL elapsed is
user-visible copy; its language follows `user_settings` ``display.language``
(user ruling 2026-08-13: one language source, no separate field), the same
mechanism as the IM alert copy (``shared/alerts_copy.py``). This module is
exempt from the repo-wide no-CJK gate (scripts/lint_no_cjk.py
``_LOCALE_PY_FILES``): raw CJK here is locale data, like the next-intl
message catalogs. Only template copy is translated — the page name and agent
id pass through untranslated.
"""

from __future__ import annotations

PAGE_LANGUAGES = ("zh", "en")
PAGE_LANGUAGE_DEFAULT = "zh"

PAGE_EXPIRED_TITLE = {
    "zh": "页面已过期",
    "en": "Page expired",
}
PAGE_EXPIRED_BODY = {
    "zh": "页面已过期，请让 agent 重新 serve",  # noqa: RUF001 - zh copy carries fullwidth punctuation
    "en": "Page expired - ask the agent to serve it again",
}
