# meiva:ref meiva/web/docs/providers.meiva.okf.md#web-deepseek-provider

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from base.host.net.ports import UrlOpener
from base.host.net.resilience import retry
from meiva.web.deps import (
    DeepSeekFetchConfig,
    DeepSeekSearchConfig,
    FetchDeps,
    RequestPacer,
    SearchDeps,
)
from meiva.web.errors import FetchError, SearchError, WebError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["RateLimiter", "read_page", "search_one"]

_log = logging.getLogger(__name__)

_DETAIL_MAX = 200


class RateLimiter:
    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            time.sleep(wait)


def _provider_message(detail: str) -> str:
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:_DETAIL_MAX]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return detail[:_DETAIL_MAX]


def _post(
    endpoint: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout_s: float,
    open_url: UrlOpener,
    pacer: RequestPacer,
    error_type: type[WebError],
    label: str,
) -> dict[str, Any]:
    payload = json.dumps(body).encode()

    def _send() -> bytes:
        pacer.acquire()
        # meiva:ref meiva/web/docs/providers.meiva.okf.md#web-deepseek-provider
        request = urllib.request.Request(  # noqa: S310
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with open_url(request, timeout_s) as response:
            return response.read()

    try:
        data: dict[str, Any] = json.loads(retry(WEB_READ_POLICY)(_send))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "%s HTTP %s after %d attempts: %s",
            label,
            e.code,
            WEB_READ_POLICY.max_attempts,
            detail[:_DETAIL_MAX],
        )
        raise error_type(f"{label} HTTP {e.code}: {_provider_message(detail)}") from e
    except urllib.error.URLError as e:
        _log.warning(
            "%s connection failed after %d attempts: %s",
            label,
            WEB_READ_POLICY.max_attempts,
            e.reason,
        )
        raise error_type(f"{label} connection failed: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise error_type(f"{label} returned non-JSON: {e}") from e
    return data


def _api_key(config: DeepSeekSearchConfig | DeepSeekFetchConfig, error_type: type[WebError]) -> str:
    if config.api_key is None:
        raise error_type(
            "DEEPSEEK_API_KEY is not set — DeepSeek Search & Fetch uses the same key as the "
            "deepseek-* models; export it or write it into .env"
        )
    return config.api_key.get_secret_value()


def search_one[Result](
    query: str, count: int, deps: SearchDeps, make_result: Callable[..., Result]
) -> list[Result]:
    config = deps.deepseek
    data = _post(
        config.endpoint,
        {"query": query, "count": max(1, min(count, config.max_count))},
        api_key=_api_key(config, SearchError),
        timeout_s=config.timeout_s,
        open_url=deps.open_url,
        pacer=config.pacer,
        error_type=SearchError,
        label="DeepSeek Search",
    )
    return [
        make_result(
            title=deps.scan(item.get("title") or "", "web.search"),
            url=item.get("url") or "",
            snippet=deps.scan(item.get("snippet") or item.get("summary") or "", "web.search"),
            kind="web",
        )
        for item in data.get("results") or []
    ]


def read_page(url: str, max_chars: int, deps: FetchDeps) -> tuple[str, str, str, bool]:
    config = deps.deepseek
    data = _post(
        config.endpoint,
        {"url": url},
        api_key=_api_key(config, FetchError),
        timeout_s=config.timeout_s,
        open_url=deps.open_url,
        pacer=config.pacer,
        error_type=FetchError,
        label="DeepSeek Fetch",
    )
    content = data.get("content") or ""
    if not content:
        raise FetchError(f"DeepSeek Fetch returned no page content for {url}")
    return data.get("title") or "", url, content[:max_chars], len(content) > max_chars
