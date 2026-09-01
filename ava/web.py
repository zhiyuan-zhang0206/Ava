__all_for_ava__ = [
    "ReasoningEffort",
    "SearchResult",
    "fetch",
    "search",
]

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ava._batch import DEFAULT_BATCH_MAX_CONCURRENT, run_batch, validate_max_concurrent
from ava._sdk_validation import coerce_str, coerce_typed
from ava.security import scan_content
from shared.config import settings
from shared.lm._call import answer_text
from shared.lm._effort import ReasoningEffort, coerce_effort
from shared.resilience import ExponentialBackoff, Policy, http_classifier, retry

_log = logging.getLogger(__name__)

# Web reads are idempotent — retry transient failures (429/5xx/transport)
# with exponential backoff + Retry-After respect (the audit-06 Q1 gap).
_WEB_READ_POLICY = Policy(
    max_attempts=3,
    backoff=ExponentialBackoff(base=1.0, factor=2.0, cap=8.0),
    classify=http_classifier,
    respect_retry_after=True,
)


class WebError(Exception):
    pass


class SearchError(WebError):
    pass


class FetchError(WebError):
    pass


# Defaults below mirror `shared.config.Settings.web_*` (env override:
# `AVA_WEB_MAX_RESULTS` / `AVA_WEB_SEARCH_TIMEOUT_SECONDS` / `AVA_WEB_FETCH_TIMEOUT_SECONDS`).
# Brave free tier cap is 20 — set higher and Brave returns 422 directly.
# Read lazily, not at import: the web domain is agent-profile-only (Task #856)
# and the gateway imports this module transitively via `ava` — a module-level
# `settings.web` read would crash a gateway-profile process at import.
def _max_count() -> int:
    """`AVA_WEB_MAX_RESULTS` — the search-result cap (Brave free tier: 20)."""
    return settings.web.web_max_search_results


def _search_timeout_s() -> float:
    """`AVA_WEB_SEARCH_TIMEOUT_SECONDS` — Brave search usually answers < 1s;
    10s covers occasional network jitter."""
    return settings.web.web_search_timeout_seconds


def _fetch_timeout_s() -> float:
    """`AVA_WEB_FETCH_TIMEOUT_SECONDS` — Jina Reader runs a headless browser +
    JS + content-extraction server-side, significantly slower than a pure HTTP
    API; 30s covers a heavy SPA cold render."""
    return settings.web.web_fetch_timeout_seconds


def _search_endpoint() -> str:
    """`AVA_WEB_BRAVE_SEARCH_ENDPOINT` — Brave Search provider endpoint.

    Read lazily like the other web settings: the web domain is
    agent-profile-only and the gateway imports this module transitively via
    `ava` (see the comment above `_max_count`)."""
    return settings.web.web_brave_search_endpoint


def _reader_base() -> str:
    """`AVA_WEB_JINA_BASE_URL` — Jina Reader base URL; the target page URL is
    appended after it."""
    return settings.web.web_jina_reader_base


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

# Domains whose wall is a hard login gate that no server-side reader can pass
# (per-request signed requests + login-only content). For these the agent must
# use the matching logged-in-browser skill instead of reading the URL here.
_GATED_SITE_SKILLS = {
    # zhihu / xiaohongshu / douyin adapters moved to private install
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    kind: str = "web"

    def __str__(self) -> str:
        return f"[{self.kind}] {self.title}\n  {self.url}\n  {self.snippet}"


def search(
    queries: list[str], count: int = 10, max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT
) -> list[list[SearchResult]]:
    """Search each query in parallel.

    `count` caps results per query (at most 20).

    `max_concurrent` caps parallel queries; the default is 12. Pass a positive
    integer to choose a different ceiling.

    Returns:
        One result list per query, in input order.
    """
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
    return asyncio.run(run_batch(queries, lambda q: _search_one(q, count), max_concurrent))


def _search_one(query: str, count: int) -> list[SearchResult]:
    """The synchronous unit of work — search Brave for one query."""
    if settings.web.brave_api_key is None:
        raise SearchError(
            "BRAVE_API_KEY environment variable not set — register for a free tier key at "
            "https://brave.com/search/api/ then export it or write it into .env"
        )

    count = max(1, min(count, _max_count()))
    url = f"{_search_endpoint()}?{urllib.parse.urlencode({'q': query, 'count': count})}"
    req = urllib.request.Request(  # noqa: S310 — endpoint comes from settings; default is an https:// literal
        url,
        headers={
            "X-Subscription-Token": settings.web.brave_api_key.get_secret_value(),
            "Accept": "application/json",
        },
    )

    def _get() -> bytes:
        with urllib.request.urlopen(req, timeout=_search_timeout_s()) as resp:  # noqa: S310
            return resp.read()

    try:
        data = json.loads(retry(_WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Brave Search HTTP %s after %d attempts: %s",
            e.code,
            _WEB_READ_POLICY.max_attempts,
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


def fetch(
    targets: list[tuple[str, str]],
    max_chars: int = 50_000,
    effort: str | ReasoningEffort | None = None,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    """Fetch web pages in parallel and answer a prompt about each.

    Each target is a `(url, prompt)` pair: the page at `url` is read, then a
    model answers `prompt` against its content. `max_chars` caps how much of
    each page the model reads.

    `effort` sets the summarizing model's reasoning depth (same levels as
    `ava.understand`; also available as `ava.web.ReasoningEffort`). `None`
    (default) keeps the configured `settings.web.web_fetch_reasoning`
    (default `none`) — pass `effort="high"` per call for deeper summaries.

    `max_concurrent` caps parallel fetches; the default is 12. Pass a positive
    integer to choose a different ceiling.

    Returns:
        One answer per pair, in input order.
    """
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

    effort = coerce_effort(effort, example="ava.web.fetch(targets, effort='high')")
    validate_max_concurrent(max_concurrent, example="ava.web.fetch(targets, max_concurrent=4)")
    # The answer step's per-invoke bound lives in the provider client
    # (build_chat_model timeout) — see ava/understand.py for why a
    # batch-layer wait_for would not actually return early. The page-read
    # step has its own AVA_WEB_FETCH_TIMEOUT_SECONDS bound.
    return asyncio.run(
        run_batch(
            targets,
            lambda pair: _fetch_one(pair[0], pair[1], max_chars, effort),
            max_concurrent,
        )
    )


def _raise_if_wall(url: str, title: str, content: str) -> None:
    """Raise FetchError when the fetched body is a bot-challenge or login wall
    rather than the page itself, so the caller never answers a prompt from
    verification-page text.

    For a host whose wall is a hard login gate, the message names the
    logged-in-browser skill to reach it instead.

    Raises:
        FetchError: the body matched a known wall (short page + wall marker).
    """
    blob = f"{title}\n{content}".lower()
    if len(content) > _WALL_MAX_LEN or not any(marker in blob for marker in _WALL_MARKERS):
        return
    host = urllib.parse.urlparse(url).netloc.lower()
    hint = ""
    for domain, skill in _GATED_SITE_SKILLS.items():
        if host == domain or host.endswith(f".{domain}"):
            hint = f" This host is login-gated — use the {skill} skill instead."
            break
    raise FetchError(
        f"The reader returned a bot-challenge or login wall for {url} instead of "
        f"the page content (title={title!r}).{hint}"
    )


def _read_page(url: str, max_chars: int) -> tuple[str, str, str, bool]:
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
    request_url = _reader_base() + encoded_target

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": _AVA_USER_AGENT,
    }
    # JINA_API_KEY is optional — with a key 20 RPM → 500 RPM; anonymous also works
    if settings.web.jina_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.web.jina_api_key.get_secret_value()}"

    req = urllib.request.Request(request_url, headers=headers)  # noqa: S310 — base comes from settings; default is an https:// literal

    def _get() -> bytes:
        with urllib.request.urlopen(req, timeout=_fetch_timeout_s()) as resp:  # noqa: S310
            return resp.read()

    try:
        data = json.loads(retry(_WEB_READ_POLICY)(_get))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "Jina Reader HTTP %s after %d attempts: %s",
            e.code,
            _WEB_READ_POLICY.max_attempts,
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
    effort: str | ReasoningEffort | None = None,
) -> str:
    """Have a model answer `prompt` against the fetched page content.

    Builds the LLM directly via `build_chat_model` using the web-fetch-specific
    model (`settings.web.web_fetch_model`, default deepseek-v4-flash) and
    reasoning effort — an explicit per-call `effort` wins, else
    `settings.web.web_fetch_reasoning` (default none) — independent of the
    agent's main model and `ava.understand`'s text model.

    Raises:
        FetchError: the model build or call failed or returned an empty answer.
    """
    cut = "\n\n[The page was longer than the read limit and was cut off here.]" if truncated else ""
    page = f"# {title}\n{url}\n\n{content}{cut}"

    model = settings.web.web_fetch_model
    resolved_effort = effort if effort is not None else settings.web.web_fetch_reasoning
    return answer_text(
        prompt,
        page,
        model=model,
        effort=resolved_effort,
        error_type=FetchError,
        desc=f"{model} for {url}",
        build_error=lambda m, e: FetchError(f"Building model {m!r} for {url} failed: {e}"),
        retry_attempts=settings.lm.llm_invoke_retry_attempts,
        retry_delay_seconds=settings.lm.llm_invoke_retry_delay_seconds,
        retry_max_delay_seconds=settings.lm.llm_invoke_retry_max_delay_seconds,
        timeout=settings.lm.llm_invoke_timeout_seconds,
    )


def _fetch_one(url: str, prompt: str, max_chars: int, effort: str | ReasoningEffort | None) -> str:
    """Read one page and answer one prompt — the synchronous unit of work.

    Wraps `_read_page` + `_answer` + `scan_content` so the asyncio layer
    can schedule multiple copies via `asyncio.to_thread`.
    """
    title, page_url, content, truncated = _read_page(url, max_chars)
    answer = _answer(
        prompt,
        title=title,
        url=page_url,
        content=content,
        truncated=truncated,
        effort=effort,
    )
    return scan_content(answer, source="web.fetch")
