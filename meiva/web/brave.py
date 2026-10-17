"""Search Brave for one query."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from base.host.net.resilience import retry
from meiva.web.deps import SearchDeps
from meiva.web.errors import SearchError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["search_one"]

_log = logging.getLogger(__name__)


def search_one[Result](
    query: str, count: int, deps: SearchDeps, make_result: Callable[..., Result]
) -> list[Result]:
    """The synchronous unit of work — search Brave for one query.

    `make_result(title=, url=, snippet=, kind=)` builds a result; the agent-visible type is the
    facade's, so the facade passes it in.
    """
    brave = deps.brave
    if brave.api_key is None:
        raise SearchError(
            "BRAVE_API_KEY environment variable not set — register for a free tier key at "
            "https://brave.com/search/api/ then export it or write it into .env"
        )

    count = max(1, min(count, brave.max_count))
    url = f"{brave.endpoint}?{urllib.parse.urlencode({'q': query, 'count': count})}"
    req = urllib.request.Request(  # noqa: S310 — endpoint comes from settings; default is an https:// literal
        url,
        headers={
            "X-Subscription-Token": brave.api_key.get_secret_value(),
            "Accept": "application/json",
        },
    )

    def _get() -> bytes:
        with deps.open_url(req, brave.timeout_s) as resp:
            return resp.read()

    try:
        data: dict[str, Any] = json.loads(retry(WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Brave Search HTTP %s after %d attempts: %s",
            e.code,
            WEB_READ_POLICY.max_attempts,
            # Error-detail excerpt for the log line: 200 chars keep one line
            # readable; the full body is not carried (task #3696 exception inventory).
            detail[:200],
        )
        raise SearchError(f"Brave Search HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        _log.warning(
            "Brave Search connection failed after %d attempts: %s",
            WEB_READ_POLICY.max_attempts,
            e.reason,
        )
        raise SearchError(f"Brave Search connection failed: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise SearchError(f"Brave Search returned non-JSON: {e}") from e

    # Brave response: multiple result-type sections (web, news, videos,
    # discussions). Collect all into a flat list, each tagged with its `kind`.
    # Per API docs, only `type` and `query` are guaranteed top-level keys;
    # each result-type section appears only when relevant data is available.
    _section_kinds: list[tuple[str, str, str]] = [
        ("web", "title", "description"),
        ("news", "title", "description"),
        ("videos", "title", "description"),
        ("discussions", "title", "description"),
    ]
    all_results: list[Result] = []
    for section, title_key, snippet_key in _section_kinds:
        for r in data.get(section, {}).get("results", []):
            all_results.append(
                make_result(
                    title=deps.scan(r.get(title_key, ""), "web.search"),
                    url=r.get("url", ""),
                    snippet=deps.scan(r.get(snippet_key, ""), "web.search"),
                    kind=section,
                )
            )
    return all_results
