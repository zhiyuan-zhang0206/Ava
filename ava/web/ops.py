"""The work behind `ava.web.search` and `ava.web.fetch`, as functions of explicit arguments.

The namespace module `ava/web/__init__.py` is the facade: signatures, docstrings and the
`__all_for_ava__` surface. It reads the settings, builds the three config objects below (frozen
dataclasses that carry only dependencies - keys, endpoints, timeouts, the model catalog) and
calls these functions with them. A test passes its own configs and fakes for the HTTP call, the
page reader and the answering model instead of replacing anything.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from ava.sdk_surface.batch import validate_max_concurrent
from ava.sdk_surface.injection_check import begin_check
from ava.sdk_surface.validation import coerce_str, coerce_typed
from ava.security import finding_delivery_active, record_model_finding, scan_content
from ava.web.types import FetchError, SearchError, SearchResult
from base.host.env.agent_slices import ModelOverrides
from base.host.net.resilience import ExponentialBackoff, Policy, http_classifier, retry
from base.lm.call import answer_text
from base.lm.effort import ReasoningEffort, coerce_effort

__all__ = [
    "AnswerConfig",
    "FetchArgs",
    "ReaderConfig",
    "SearchArgs",
    "SearchConfig",
    "answer",
    "fetch_args",
    "fetch_one",
    "raise_if_wall",
    "read_page",
    "search_args",
    "search_one",
    "urlopen_bytes",
]

_log = logging.getLogger(__name__)

# Web reads are idempotent — retry transient failures (429/5xx/transport)
# with exponential backoff + Retry-After respect (the audit-06 Q1 gap).
_WEB_READ_POLICY = Policy(
    max_attempts=3,
    backoff=ExponentialBackoff(base=1.0, factor=2.0, cap=8.0),
    classify=http_classifier,
    respect_retry_after=True,
)


# Python urllib default UA (`Python-urllib/3.x`) is blocked by Cloudflare
# (`browser_signature_banned`); r.jina.ai sits behind Cloudflare — no UA
# results in 403. An honest identifier UA passes through. Brave Search API
# does not go through Cloudflare and is unaffected.
_AVA_USER_AGENT = "ava-sdk/1.0"

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


type Http = Callable[[urllib.request.Request, float], bytes]


def urlopen_bytes(req: urllib.request.Request, timeout: float) -> bytes:
    """The default HTTP call: one `urlopen` and the body."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


@dataclass(frozen=True)
class SearchConfig:
    """What one Brave search needs: the key, the endpoint, the timeout and the result cap."""

    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int


@dataclass(frozen=True)
class ReaderConfig:
    """What one Jina Reader fetch needs: the base URL, the timeout and the optional key."""

    base: str
    timeout_s: float
    api_key: SecretStr | None


@dataclass(frozen=True)
class AnswerConfig:
    """What the answering model call needs: the model, its defaults and the catalog."""

    model: str
    default_effort: str | ReasoningEffort
    retry_attempts: int
    retry_delay_seconds: float
    retry_max_delay_seconds: float
    timeout: float | None
    llm_override: str
    catalog: Any
    overrides: ModelOverrides


@dataclass(frozen=True)
class SearchArgs:
    """Validated arguments of one `search` call."""

    queries: list[str]
    count: int
    max_concurrent: int | None


@dataclass(frozen=True)
class FetchArgs:
    """Validated arguments of one `fetch` call."""

    targets: list[tuple[str, str]]
    max_chars: int
    effort: str | ReasoningEffort | None
    max_concurrent: int | None


def search_args(queries: list[str], count: int, max_concurrent: int | None) -> SearchArgs:
    """Check `ava.web.search`'s arguments; no I/O, no settings."""
    if not isinstance(queries, list):
        raise TypeError(
            f"search() takes a list of query strings, got {type(queries).__name__}. "
            f"Example: ava.web.search(['python asyncio tutorial'])"
        )
    for i, q in enumerate(queries):
        if not isinstance(q, str):
            raise TypeError(f"queries[{i}] must be a str, got {type(q).__name__}")
    count = coerce_typed(count, "count", int)
    validate_max_concurrent(max_concurrent, example="ava.web.search(queries, max_concurrent=4)")
    return SearchArgs(queries=queries, count=count, max_concurrent=max_concurrent)


def fetch_args(
    targets: list[tuple[str, str]],
    max_chars: int,
    effort: str | ReasoningEffort | None,
    max_concurrent: int | None,
) -> FetchArgs:
    """Check `ava.web.fetch`'s arguments; no I/O, no settings."""
    if not isinstance(targets, list):
        raise TypeError(
            f"fetch() takes a list of (url, prompt) pairs, got {type(targets).__name__}. "
            f"Example: ava.web.fetch([('https://example.com', 'summarize this page')])"
        )
    max_chars = coerce_typed(max_chars, "max_chars", int)
    effort = coerce_str(effort, "effort", allow_none=True)
    for i, pair in enumerate(targets):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(f"targets[{i}] must be a (url, prompt) pair, got {type(pair).__name__}")
        url, prompt = pair
        if not isinstance(url, str) or not isinstance(prompt, str):
            raise TypeError(
                f"targets[{i}] must be (url: str, prompt: str), "
                f"got ({type(url).__name__}, {type(prompt).__name__})"
            )
    resolved = coerce_effort(effort, example="ava.web.fetch(targets, effort='high')")
    validate_max_concurrent(max_concurrent, example="ava.web.fetch(targets, max_concurrent=4)")
    return FetchArgs(
        targets=targets, max_chars=max_chars, effort=resolved, max_concurrent=max_concurrent
    )


def search_one(
    query: str, count: int, *, cfg: SearchConfig, http: Http = urlopen_bytes
) -> list[SearchResult]:
    """The synchronous unit of work — search Brave for one query."""
    if cfg.api_key is None:
        raise SearchError(
            "BRAVE_API_KEY environment variable not set — register for a free tier key at "
            "https://brave.com/search/api/ then export it or write it into .env"
        )

    count = max(1, min(count, cfg.max_count))
    url = f"{cfg.endpoint}?{urllib.parse.urlencode({'q': query, 'count': count})}"
    req = urllib.request.Request(  # noqa: S310 — endpoint comes from settings; default is an https:// literal
        url,
        headers={
            "X-Subscription-Token": cfg.api_key.get_secret_value(),
            "Accept": "application/json",
        },
    )

    def _get() -> bytes:
        return http(req, cfg.timeout_s)

    try:
        data = json.loads(retry(_WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Brave Search HTTP %s after %d attempts: %s",
            e.code,
            _WEB_READ_POLICY.max_attempts,
            # Error-detail excerpt for the log line: 200 chars keep one line
            # readable; the full body is not carried (task #3696 exception inventory).
            detail[:200],
        )
        raise SearchError(f"Brave Search HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        _log.warning(
            "Brave Search connection failed after %d attempts: %s",
            _WEB_READ_POLICY.max_attempts,
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
    all_results: list[SearchResult] = []
    for section, title_key, snippet_key in _section_kinds:
        for r in data.get(section, {}).get("results", []):
            all_results.append(
                SearchResult(
                    title=scan_content(r.get(title_key, ""), source="web.search"),
                    url=r.get("url", ""),
                    snippet=scan_content(r.get(snippet_key, ""), source="web.search"),
                    kind=section,
                )
            )
    return all_results


def raise_if_wall(url: str, title: str, content: str) -> None:
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


def read_page(
    url: str, max_chars: int, *, cfg: ReaderConfig, http: Http = urlopen_bytes
) -> tuple[str, str, str, bool]:
    """Fetch the page main body as markdown. Returns `(title, url, content,
    truncated)` where `truncated` is True when the body exceeded `max_chars`.

    Raises:
        FetchError: reader HTTP error, network failure, bad response, the
            upstream URL was unreachable, or the body came back as a
            bot-challenge / login wall instead of the page.
    """
    # URL must be safely encoded — Jina docs say "don't forget to encode".
    # Safe keeps common URL characters to avoid double-encoding; special chars
    # (spaces / CJK query) get quoted to %xx
    encoded_target = urllib.parse.quote(url, safe=":/?#&=.@+_")
    request_url = cfg.base + encoded_target

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": _AVA_USER_AGENT,
    }
    # JINA_API_KEY is optional — with a key 20 RPM → 500 RPM; anonymous also works
    if cfg.api_key is not None:
        headers["Authorization"] = f"Bearer {cfg.api_key.get_secret_value()}"

    req = urllib.request.Request(request_url, headers=headers)  # noqa: S310 — base comes from settings; default is an https:// literal

    def _get() -> bytes:
        return http(req, cfg.timeout_s)

    try:
        data = json.loads(retry(_WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Jina Reader HTTP %s after %d attempts: %s",
            e.code,
            _WEB_READ_POLICY.max_attempts,
            # Same excerpt cap as the search path above (task #3696 exception inventory).
            detail[:200],
        )
        raise FetchError(f"Jina Reader HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        _log.warning(
            "Jina Reader connection failed after %d attempts: %s",
            _WEB_READ_POLICY.max_attempts,
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
    raise_if_wall(url, title, content)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return title, page_url, content, truncated


def answer(
    prompt: str,
    *,
    title: str,
    url: str,
    content: str,
    truncated: bool,
    effort: str | ReasoningEffort | None = None,
    cfg: AnswerConfig,
) -> str:
    """Have a model answer `prompt` against the fetched page content.

    Builds the LLM directly via `build_chat_model` using the web-fetch-specific
    model (`settings.web.web_fetch_model`, default deepseek-flash) and
    reasoning effort — an explicit per-call `effort` wins, else
    `settings.web.web_fetch_reasoning` (default none) — independent of the
    agent's main model and `ava.understand`'s text model.

    Raises:
        FetchError: the model build failed, a typed provider call failed
            permanently or exhausted its retries, or the model returned an
            empty answer. Unknown model invocation errors retain their original
            type without retries.
    """
    cut = "\n\n[The page was longer than the read limit and was cut off here.]" if truncated else ""
    page = f"# {title}\n{url}\n\n{content}{cut}"

    model = cfg.model
    resolved_effort = effort if effort is not None else cfg.default_effort
    return answer_text(
        prompt,
        page,
        model=model,
        effort=resolved_effort,
        error_type=FetchError,
        desc=f"{model} for {url}",
        build_error=lambda m, e: FetchError(f"Building model {m!r} for {url} failed: {e}"),
        retry_attempts=cfg.retry_attempts,
        retry_delay_seconds=cfg.retry_delay_seconds,
        retry_max_delay_seconds=cfg.retry_max_delay_seconds,
        timeout=cfg.timeout,
        usage_source="web.fetch",
        catalog=cfg.catalog,
        llm_override=cfg.llm_override,
        overrides=cfg.overrides,
    )


def fetch_one(
    url: str,
    prompt: str,
    max_chars: int,
    effort: str | ReasoningEffort | None,
    *,
    read: Callable[[str, int], tuple[str, str, str, bool]],
    ask: Callable[..., str],
) -> str:
    """Read one page and answer one prompt - the synchronous unit of work.

    `read` fetches the page (`read_page` with its config bound) and `ask` answers the prompt
    (`answer` with its config bound). Wraps them with the injection check and `scan_content`
    so the asyncio layer can schedule multiple copies via `asyncio.to_thread`. The check
    judges the page text itself, which `scan_content` on the summary never sees; it
    needs only the page, so it runs beside `ask`.
    """
    title, page_url, content, truncated = read(url, max_chars)
    finish_check = begin_check(
        url,
        title,
        content,
        delivery_active=finding_delivery_active,
        record=record_model_finding,
    )
    try:
        reply = ask(
            prompt,
            title=title,
            url=page_url,
            content=content,
            truncated=truncated,
            effort=effort,
        )
    finally:
        finish_check()
    return scan_content(reply, source="web.fetch")
