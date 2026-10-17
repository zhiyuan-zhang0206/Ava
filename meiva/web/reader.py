"""Fetch one page through Jina Reader and have a model answer a prompt about it."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from base.host.net.resilience import retry
from base.lm.call import answer_text
from base.lm.effort import ReasoningEffort
from meiva.web import deepseek
from meiva.web.deps import FetchDeps
from meiva.web.errors import FetchError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["fetch_one"]

_log = logging.getLogger(__name__)

# Python urllib default UA (`Python-urllib/3.x`) is blocked by Cloudflare
# (`browser_signature_banned`); r.jina.ai sits behind Cloudflare — no UA
# results in 403. An honest identifier UA passes through. Brave Search API
# does not go through Cloudflare and is unaffected.
_MEIVA_USER_AGENT = "meiva-sdk/1.0"

# A reader fetch can come back HTTP 200 with the site's bot-challenge or
# login-wall page as the body instead of the article. The body then looks like
# a success, so without this check the wall text is fed to the model, which
# "answers" the prompt from a verification notice (seen on Cloudflare-protected
# sites and login-gated social sites). These markers identify the common walls;
# a wall body is always tiny, so a length cap keeps an article that merely
# mentions one of the phrases from being flagged.
_WALL_MARKERS = (
    "just a moment...",
    "performing security verification",
    "security service to protect against malicious bots",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "verify you are human",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u8bf7\u60a8\u767b\u5f55\u540e\u67e5\u770b",
    "\u767b\u5f55\u540e\u67e5\u770b\u66f4\u591a",
)
_WALL_MAX_LEN = 2_000


def _raise_if_wall(url: str, title: str, content: str) -> None:
    """Raise FetchError when the fetched body is a bot-challenge or login wall
    rather than the page itself, so the caller never answers a prompt from
    verification-page text.

    Raises:
        FetchError: the body matched a known wall (short page + wall marker).
    """
    blob = f"{title}\n{content}".lower()
    if len(content) > _WALL_MAX_LEN or not any(marker in blob for marker in _WALL_MARKERS):
        return
    raise FetchError(
        f"The reader returned a bot-challenge or login wall for {url} instead of "
        f"the page content (title={title!r})."
    )


def _read_page(url: str, max_chars: int, deps: FetchDeps) -> tuple[str, str, str, bool]:
    """Fetch the page main body as markdown. Returns `(title, url, content,
    truncated)` where `truncated` is True when the body exceeded `max_chars`.

    Raises:
        FetchError: reader HTTP error, network failure, bad response, the
            upstream URL was unreachable, or the body came back as a
            bot-challenge / login wall instead of the page.
    """
    jina = deps.jina
    # URL must be safely encoded — Jina docs say "don't forget to encode".
    # Safe keeps common URL characters to avoid double-encoding; special chars
    # (spaces / CJK query) get quoted to %xx
    encoded_target = urllib.parse.quote(url, safe=":/?#&=.@+_")
    request_url = jina.reader_base + encoded_target

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": _MEIVA_USER_AGENT,
    }
    # JINA_API_KEY is optional — with a key 20 RPM → 500 RPM; anonymous also works
    if jina.api_key is not None:
        headers["Authorization"] = f"Bearer {jina.api_key.get_secret_value()}"

    req = urllib.request.Request(request_url, headers=headers)  # noqa: S310 — base comes from settings; default is an https:// literal

    def _get() -> bytes:
        with deps.open_url(req, jina.timeout_s) as resp:
            return resp.read()

    try:
        data: dict[str, Any] = json.loads(retry(WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Jina Reader HTTP %s after %d attempts: %s",
            e.code,
            WEB_READ_POLICY.max_attempts,
            # Same excerpt cap as the search path above (task #3696 exception inventory).
            detail[:200],
        )
        raise FetchError(f"Jina Reader HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        _log.warning(
            "Jina Reader connection failed after %d attempts: %s",
            WEB_READ_POLICY.max_attempts,
            e.reason,
        )
        raise FetchError(f"Jina Reader connection failed: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise FetchError(f"Jina Reader returned non-JSON: {e}") from e

    # Jina business errors (upstream URL unreachable / rejected etc.) use
    # response.code to distinguish HTTP layer vs application layer — when
    # code != 200 the data fields may be empty; raise so the agent sees the
    # specific reason
    code = data.get("code")
    if code != 200:
        msg = data.get("message") or data.get("status") or "unknown"
        raise FetchError(f"Jina Reader fetch failed (code={code}): {msg}")

    # Jina JSON response shape (observed 2026-04):
    # `{code, status, data: {title, description, url, content, metadata, external, usage}}`
    payload = data.get("data", {})
    content = payload.get("content", "")
    title = payload.get("title", "")
    page_url = payload.get("url", url)
    _raise_if_wall(url, title, content)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return title, page_url, content, truncated


def _answer(
    prompt: str,
    *,
    title: str,
    url: str,
    content: str,
    truncated: bool,
    effort: str | ReasoningEffort | None,
    deps: FetchDeps,
) -> str:
    """Have a model answer `prompt` against the fetched page content.

    Builds the LLM directly via the provider factory using the web-fetch-specific
    model (`settings.web.web_fetch_model`, default deepseek-flash) and
    reasoning effort — an explicit per-call `effort` wins, else
    `settings.web.web_fetch_reasoning` (default none) — independent of the
    agent's main model and `meiva.understand`'s text model.

    Raises:
        FetchError: the model build failed, a typed provider call failed
            permanently or exhausted its retries, or the model returned an
            empty answer. Unknown model invocation errors retain their original
            type without retries.
    """
    cut = "\n\n[The page was longer than the read limit and was cut off here.]" if truncated else ""
    page = f"# {title}\n{url}\n\n{content}{cut}"

    fm = deps.fetch_model
    return answer_text(
        prompt,
        page,
        model=fm.model,
        effort=effort if effort is not None else fm.reasoning,
        error_type=FetchError,
        desc=f"{fm.model} for {url}",
        build_error=lambda m, e: FetchError(f"Building model {m!r} for {url} failed: {e}"),
        retry_attempts=fm.retry_attempts,
        retry_delay_seconds=fm.retry_delay_seconds,
        retry_max_delay_seconds=fm.retry_max_delay_seconds,
        timeout=fm.timeout_seconds,
        usage_source="web.fetch",
        catalog=fm.catalog,
        llm_override=fm.llm_override,
        overrides=fm.overrides,
        build_chat_model=deps.build_chat_model,
    )


def read_page(url: str, max_chars: int, deps: FetchDeps) -> tuple[str, str, str, bool]:
    # meiva:link meiva/web/docs/providers.meiva.okf.md
    if deps.reader == "jina":
        return _read_page(url, max_chars, deps)
    return deepseek.read_page(url, max_chars, deps)


def fetch_one(
    url: str, prompt: str, max_chars: int, effort: str | ReasoningEffort | None, deps: FetchDeps
) -> str:
    """Read one page and answer one prompt — the synchronous unit of work.

    Wraps `_read_page` + the injection check + `_answer` + the output scan so the
    asyncio layer can schedule multiple copies via `asyncio.to_thread`. The check
    judges the page text itself, which the scan of the summary never sees; it
    needs only the page, so it runs beside `_answer`.
    """
    title, page_url, content, truncated = read_page(url, max_chars, deps)
    finish_check = deps.begin_check(url, title, content)
    try:
        answer = _answer(
            prompt,
            title=title,
            url=page_url,
            content=content,
            truncated=truncated,
            effort=effort,
            deps=deps,
        )
    finally:
        finish_check()
    return deps.scan(answer, "web.fetch")
