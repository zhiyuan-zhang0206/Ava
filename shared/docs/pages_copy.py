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

# 502 (page server unreachable): the gateway's reverse proxy could not dial
# the registered page server. The user-facing hint says what happened and
# what fixes it; the technical detail (host:port, exception) stays in the
# gateway log, not in the response (task #2212).
PAGE_SERVER_DOWN_BODY = {
    "zh": "页面服务器刚重启或已失效——让生成它的 agent 重新发布即可恢复",
    "en": "The page server just restarted or is no longer available - ask the agent that created it to republish",
}

# 504 (page server timed out): the proxy dialed or waited but the server did
# not answer in time. The server is up but stuck (or the dial hung); the fix
# is the same as 502 — the agent republishes.
PAGE_SERVER_TIMEOUT_BODY = {
    "zh": "页面服务器无响应（超时）——让生成它的 agent 重新发布即可恢复",  # noqa: RUF001 - zh copy carries fullwidth punctuation
    "en": "The page server did not respond in time - ask the agent that created it to republish",
}
