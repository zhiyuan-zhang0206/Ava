"""`ava.web.search` and `ava.web.fetch` tests.

Search tests: Brave Search API urllib thin wrapper — no real API calls,
monkeypatch `urllib.request.urlopen`.

Fetch tests: Jina Reader + ava.understand — no real API or LLM calls,
monkeypatch `urllib.request.urlopen` and `ava._understand.understand`.

Key contracts:
- fetch() only accepts list[tuple[str, str]], raises TypeError otherwise
- Batch fetch runs in parallel via asyncio, preserves input order
- Concurrency is unbounded by default; an optional `max_concurrent` caps how
  many items are in flight at once. A provider 429 propagates as
  SearchError / FetchError rather than being throttled away
- Individual fetch contracts (truncation, wall detection, error wrapping) still hold
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

import ava
from ava.web import FetchError, SearchError, WebError
from shared.config import settings


class _FakeResp:
    """Minimum urllib.request.urlopen return object — supports `with` + `.read()`."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._payload


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize shared.resilience backoff sleeps so retry-path tests run
    instantly; the retry loop itself is still exercised (call counts)."""
    monkeypatch.setattr("shared.resilience._sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]


def _make_brave_response(results: list[dict]) -> bytes:
    return json.dumps({"web": {"results": results}}).encode()


def test_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "brave_api_key", None)
    with pytest.raises(SearchError, match="BRAVE_API_KEY"):
        ava.web.search(["anything"])


def test_parses_brave_response_into_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    payload = _make_brave_response(
        [
            {
                "title": "Python asyncio docs",
                "url": "https://docs.python.org/3/library/asyncio.html",
                "description": "The asyncio library...",
            },
            {
                "title": "Real Python async tutorial",
                "url": "https://realpython.com/async-io-python/",
                "description": "Hands-on intro...",
            },
        ]
    )
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        results = ava.web.search(["python asyncio"])
    assert len(results) == 1
    assert len(results[0]) == 2
    assert isinstance(results[0][0], ava.web.SearchResult)
    assert results[0][0].title == "Python asyncio docs"
    assert results[0][0].url == "https://docs.python.org/3/library/asyncio.html"
    assert results[0][0].snippet == "The asyncio library..."
    assert results[0][0].kind == "web"


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    payload = _make_brave_response([])
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        assert ava.web.search(["nonsense query qqqq"]) == [[]]


def test_missing_web_section_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brave response missing the `web` key — per API docs only `type` and `query`
    are guaranteed; `web` and other result-type keys appear only when relevant
    data is available. Missing `web` = no web results → return empty list."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    with patch(
        "ava.web.urllib.request.urlopen",
        return_value=_FakeResp(
            b'{"type": "search", "query": {"original": "q"}, "news": {"results": []}}'
        ),
    ):
        results = ava.web.search(["query"])
    assert results == [[]]


def test_collects_results_from_all_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() collects from web, news, videos — each tagged with kind."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    payload = json.dumps(
        {
            "web": {"results": [{"title": "W", "url": "https://a.com", "description": "d"}]},
            "news": {"results": [{"title": "N", "url": "https://b.com", "description": "d"}]},
            "videos": {"results": [{"title": "V", "url": "https://c.com", "description": "d"}]},
        }
    ).encode()
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        results = ava.web.search(["query"])
    assert len(results) == 1
    assert len(results[0]) == 3
    assert {r.kind for r in results[0]} == {"web", "news", "videos"}
    assert results[0][0].kind == "web"
    assert results[0][1].kind == "news"
    assert results[0][2].kind == "videos"


def test_http_error_raises_with_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """API returns 422/429/500 etc., must include upstream body in traceback — agent can
    diagnose (rate limit / invalid param / expired key)."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url="https://api.search.brave.com/res/v1/web/search",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    with (
        patch("ava.web.urllib.request.urlopen", side_effect=http_err),
        pytest.raises(SearchError, match="Brave Search HTTP 429"),
    ):
        ava.web.search(["query"])


def test_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    url_err = urllib.error.URLError("nodename nor servname provided")
    with (
        patch("ava.web.urllib.request.urlopen", side_effect=url_err),
        pytest.raises(WebError, match="connection failed"),
    ):
        ava.web.search(["query"])


def test_transient_429_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient 429 is retried (R2-D): the search succeeds once the
    upstream recovers — pre-R2 a 429 failed the whole search."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url="https://api.search.brave.com/res/v1/web/search",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    calls = {"n": 0}

    def _flaky(*_args: object, **_kwargs: object) -> _FakeResp:
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_err
        return _FakeResp(
            _make_brave_response([{"title": "t", "url": "https://a.com", "description": "d"}])
        )

    with patch("ava.web.urllib.request.urlopen", side_effect=_flaky):
        results = ava.web.search(["query"])
    assert calls["n"] == 3
    assert results[0][0].title == "t"


def test_persistent_429_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 429 exhausts the budget and surfaces as SearchError —
    never silently swallowed (R2-D D3: idempotent reads fail loud)."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url="https://api.search.brave.com/res/v1/web/search",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    calls = {"n": 0}

    def _always(*_args: object, **_kwargs: object) -> _FakeResp:
        calls["n"] += 1
        raise http_err

    with (
        patch("ava.web.urllib.request.urlopen", side_effect=_always),
        pytest.raises(SearchError, match="Brave Search HTTP 429"),
    ):
        ava.web.search(["query"])
    assert calls["n"] == 3  # max_attempts


def test_malformed_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    with (
        patch(
            "ava.web.urllib.request.urlopen",
            return_value=_FakeResp(b"not json at all"),
        ),
        pytest.raises(WebError, match="non-JSON"),
    ):
        ava.web.search(["query"])


def test_count_clamped_to_brave_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """count > 20 would be rejected by Brave with 422; we clamp to 20 beforehand."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.search(["x"], count=100)
    assert "count=20" in captured["url"]


def test_count_floor_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """count <= 0 clamped to 1 — Brave returns 422 for count=0."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.search(["x"], count=0)
    assert "count=1" in captured["url"]


def test_subscription_token_header_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brave authentication uses `X-Subscription-Token` header — missing it gets 401, must verify."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("my-secret-key"))
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.search(["x"])
    # urllib capitalizes header names; Brave accepts `X-Subscription-Token`
    assert captured["headers"].get("X-subscription-token") == "my-secret-key"


def test_search_result_str_format() -> None:
    """The format of `print(r)` for later LLM context when concatenating multiple results — kind prefix + 3-line block stable."""
    r = ava.web.SearchResult(title="Foo", url="https://example.com", snippet="A foo page.")
    assert str(r) == "[web] Foo\n  https://example.com\n  A foo page."


# ─── search signature ───


def test_search_requires_list() -> None:
    """search() only accepts list[str]. Passing a bare string raises TypeError."""
    with pytest.raises(TypeError, match=r"list of query strings"):
        ava.web.search("not a list")  # type: ignore[arg-type]


def test_search_rejects_non_string_elements() -> None:
    with pytest.raises(TypeError, match=r"must be a str"):
        ava.web.search(["ok", 123])  # type: ignore[list-item]


# ─── batch search (concurrency) ───


def test_search_batch_returns_results_in_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple queries: results come back in the same order as the input list."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))

    call_order: list[str] = []

    def _side_effect(req: Any, timeout: Any):
        url = req.full_url
        call_order.append(url)
        if "q=a" in url:
            return _FakeResp(
                _make_brave_response([{"title": "A", "url": "https://a.com", "description": "d"}])
            )
        if "q=b" in url:
            return _FakeResp(
                _make_brave_response([{"title": "B", "url": "https://b.com", "description": "d"}])
            )
        if "q=c" in url:
            return _FakeResp(
                _make_brave_response([{"title": "C", "url": "https://c.com", "description": "d"}])
            )
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_side_effect):
        results = ava.web.search(["a", "b", "c"])

    assert len(results) == 3
    assert results[0][0].title == "A"
    assert results[1][0].title == "B"
    assert results[2][0].title == "C"
    assert len(call_order) == 3


def test_search_batch_runs_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch search must run in parallel — total time close to one search latency."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))

    barrier = threading.Barrier(3, timeout=5)

    def _slow_search(req, timeout):
        barrier.wait()
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_slow_search):
        t0 = time.monotonic()
        results = ava.web.search(["q1", "q2", "q3"])
        elapsed = time.monotonic() - t0

    assert results == [[], [], []]
    assert elapsed < 3.0, f"Search took {elapsed:.1f}s — should be concurrent (< 3s)"


def test_search_signature_includes_max_concurrent() -> None:
    """`max_concurrent` is an optional ceiling on in-flight queries — None
    (the default) keeps unbounded concurrency, and rate limiting stays with
    the provider (a 429 surfaces as SearchError)."""
    import inspect

    sig = inspect.signature(ava.web.search)
    assert list(sig.parameters) == ["queries", "count", "max_concurrent"]
    assert sig.parameters["max_concurrent"].default is None


def test_search_batch_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty queries list returns empty results list."""
    results = ava.web.search([])
    assert results == []


def test_search_max_concurrent_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_concurrent must be a positive int or None — bad values fail fast
    before any search call."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    with pytest.raises(ValueError, match="at least 1"):
        ava.web.search(["q"], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        ava.web.search(["q"], max_concurrent=-3)
    with pytest.raises(TypeError, match="int or None"):
        ava.web.search(["q"], max_concurrent=2.5)  # type: ignore[arg-type]


def test_search_max_concurrent_caps_inflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_concurrent=N keeps at most N searches in flight — the observed peak
    never exceeds the ceiling, and results still come back in input order."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))

    lock = threading.Lock()
    inflight = 0
    peak = 0

    def _tracking_search(req, timeout):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        return _FakeResp(_make_brave_response([]))

    with patch("ava.web.urllib.request.urlopen", side_effect=_tracking_search):
        results = ava.web.search(["q1", "q2", "q3", "q4", "q5", "q6"], max_concurrent=2)
    assert results == [[], [], [], [], [], []]
    assert peak == 2, f"peak in-flight {peak} exceeded ceiling 2"


# ─── ava.web.fetch (Jina Reader) ───


def _make_jina_response(
    *,
    title: str = "",
    url: str = "",
    content: str = "",
    code: int = 200,
) -> bytes:
    return json.dumps(
        {
            "code": code,
            "data": {"title": title, "url": url, "content": content},
        }
    ).encode()


class _FakeLLMResponse:
    """Minimal AIMessage stand-in: carries a string `.content` plus empty
    `.response_metadata` so `_answer`'s text extraction works unchanged."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.response_metadata: dict[str, Any] = {}


class _FakeLLM:
    """Stand-in LangChain ChatModel — `invoke` captures content blocks then
    returns `captured["answer"]` (default "FAKE ANSWER") or raises
    `captured["error"]` when a test sets one."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def invoke(self, messages: list[Any]) -> _FakeLLMResponse:
        for msg in messages:
            if hasattr(msg, "content") and isinstance(msg.content, list):
                for block in msg.content:  # pyright: ignore[reportUnknownMemberType]
                    if isinstance(block, dict) and block.get("type") == "text":  # pyright: ignore[reportUnknownMemberType]
                        txt = block["text"]  # pyright: ignore[reportUnknownMemberType]
                        if "input" not in self._captured:
                            self._captured["input"] = txt
                        else:
                            self._captured["prompt_text"] = txt
        err = self._captured.get("error")
        if err is not None:
            raise err
        return _FakeLLMResponse(self._captured.get("answer", "FAKE ANSWER"))


@pytest.fixture
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `shared.lm.factory.build_chat_model` so fetch's answer step never
    calls a real LLM. Captures the model, reasoning_effort, and input content
    blocks; returns "FAKE ANSWER" by default, or raises `captured["error"]`
    when a test sets one."""
    captured: dict[str, Any] = {}

    def _fake_build(
        model: str,
        *,
        thinking: object = None,
        reasoning_effort: object = None,
        streaming: object = None,
        timeout: float | None = None,
    ) -> _FakeLLM:
        captured["model"] = model
        captured["reasoning_effort"] = reasoning_effort
        return _FakeLLM(captured)

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _fake_build)
    return captured


# ─── fetch signature ───


def test_fetch_requires_list() -> None:
    """fetch() only accepts list[tuple[str, str]]. Passing a bare URL raises
    TypeError with a helpful message."""
    with pytest.raises(TypeError, match=r"list of .*url, prompt.* pairs"):
        ava.web.fetch("https://example.com", "summarize")  # type: ignore[call-overload]


def test_fetch_rejects_non_tuple_elements() -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        ava.web.fetch(["https://example.com"])  # type: ignore[list-item]


def test_fetch_rejects_wrong_tuple_length() -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        ava.web.fetch([("https://example.com", "prompt", "extra")])  # type: ignore[list-item]


def test_fetch_rejects_non_string_tuple_elements() -> None:
    with pytest.raises(TypeError, match=r"must be .*url: str, prompt: str"):
        ava.web.fetch([(123, "prompt")])  # type: ignore[list-item]


# ─── single-target fetch (backward-compatible behavior via list) ───


def test_fetch_reads_page_then_calls_llm_with_web_model(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Happy path: page body is fetched, framed, and handed to the web-fetch
    model (default deepseek-v4-flash with reasoning=none); its answer is what
    fetch returns."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)  # anonymous
    payload = _make_jina_response(
        title="PEP 758",
        url="https://peps.python.org/pep-0758/",
        content="Author: Pablo...\n\nAbstract\n--------\nexcept* without parens.",
    )
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        results = ava.web.fetch(
            [("https://peps.python.org/pep-0758/", "what does this PEP propose?")]
        )
    assert results == ["FAKE ANSWER"]
    # Uses the web-fetch model, not the understand text model
    assert mock_llm["model"] == "deepseek-v4-flash"
    assert mock_llm["reasoning_effort"] == "none"
    # the prompt is passed through; the framed page (title + body) is the material
    assert mock_llm["prompt_text"] == "what does this PEP propose?"
    assert "except* without parens." in mock_llm["input"]
    assert "PEP 758" in mock_llm["input"]  # title included for context


def test_fetch_tells_llm_when_content_truncated(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """content > max_chars: body is cut AND the LLM is told it was cut, so the
    answer can reflect incompleteness instead of silently dropping it."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="x" * 100_000)
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")], max_chars=1000)
    material = mock_llm["input"]
    assert "cut off" in material
    # only max_chars of the body made it in (plus the cut note + framing)
    assert material.count("x") == 1000


def test_fetch_no_truncation_note_when_under_limit(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="short")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")])
    assert "cut off" not in mock_llm["input"]


def test_fetch_wraps_llm_error(monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]) -> None:
    """LLM invoke raising (rate limit, empty response, etc.) surfaces as
    FetchError carrying the reason — never a silent failure."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    mock_llm["error"] = RuntimeError("rate limit exceeded")
    payload = _make_jina_response(content="ok")
    with (
        patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)),
        pytest.raises(FetchError, match="rate limit"),
    ):
        ava.web.fetch([("https://example.com", "summarize")])


def test_fetch_uses_jina_api_key_when_set(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """When JINA_API_KEY is set, add Authorization: Bearer header — 20 RPM → 500 RPM."""
    monkeypatch.setattr(settings.web, "jina_api_key", SecretStr("jina-secret"))
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.fetch([("https://example.com", "summarize")])
    # urllib capitalizes header names
    assert captured["headers"].get("Authorization") == "Bearer jina-secret"


def test_fetch_anonymous_no_auth_header(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Works without JINA_API_KEY — no Authorization header. Jina anonymous 20 RPM."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.fetch([("https://example.com", "summarize")])
    assert "Authorization" not in captured["headers"]


def test_fetch_sends_custom_user_agent(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Python urllib default UA (`Python-urllib/3.x`) is blocked by Cloudflare;
    r.jina.ai goes through Cloudflare — without custom UA gets 403 browser_signature_banned.
    Protection test added after hitting this in practice."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.fetch([("https://example.com", "summarize")])
    ua = captured["headers"].get("User-agent", "")
    assert "ava-sdk" in ua


def test_fetch_url_encodes_target(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Target URL with spaces / Chinese query must be encoded; `r.jina.ai/` prefix unchanged."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.fetch([("https://example.com/search?q=hello world", "summarize")])
    # spaces should become %20, other URL structure (scheme / :/ / ?&=) preserved
    assert captured["url"].startswith("https://r.jina.ai/https://example.com/")
    assert "%20" in captured["url"]
    assert "?q=hello" in captured["url"]


def test_fetch_jina_business_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jina response code != 200 raises — upstream URL unreachable, blocked crawling, etc.;
    agent can fallback to curl or tell the user. Raises before the model step, so no
    LLM mock needed."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = json.dumps({"code": 422, "message": "Unable to fetch: 404 Not Found"}).encode()
    with (
        patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)),
        pytest.raises(FetchError, match="Jina Reader fetch failed"),
    ):
        ava.web.fetch([("https://example.com/nonexistent", "summarize")])


def test_fetch_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP layer error (429 rate limit / 503 downstream down) must include upstream body in traceback."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    http_err = urllib.error.HTTPError(
        url="https://r.jina.ai/...",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"rate limit exceeded"),
    )
    with (
        patch("ava.web.urllib.request.urlopen", side_effect=http_err),
        pytest.raises(FetchError, match="Jina Reader HTTP 429"),
    ):
        ava.web.fetch([("https://example.com", "summarize")])


def test_fetch_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    url_err = urllib.error.URLError("nodename nor servname provided")
    with (
        patch("ava.web.urllib.request.urlopen", side_effect=url_err),
        pytest.raises(FetchError, match="Jina Reader connection failed"),
    ):
        ava.web.fetch([("https://example.com", "summarize")])


def test_fetch_malformed_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    with (
        patch(
            "ava.web.urllib.request.urlopen",
            return_value=_FakeResp(b"not json"),
        ),
        pytest.raises(WebError, match="non-JSON"),
    ):
        ava.web.fetch([("https://example.com", "summarize")])


# ─── bot-challenge / login-wall detection ───


def test_fetch_raises_on_cloudflare_wall(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Jina returns code=200 with a Cloudflare challenge page as the body; that
    used to be silently answered by the model. Now it raises so the caller knows
    the fetch failed, and the model step is never reached."""
    monkeypatch.setattr(settings.web, "jina_api_key", SecretStr("k"))
    payload = _make_jina_response(
        title="Just a moment...",
        content="Performing security verification\nThis website uses a security service "
        "to protect against malicious bots.",
    )
    with (
        patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)),
        pytest.raises(FetchError, match="bot-challenge or login wall"),
    ):
        ava.web.fetch([("https://cobusgreyling.medium.com/some-article", "summarize")])
    assert "input" not in mock_llm  # model step never ran


def test_fetch_login_gated_site_names_skill(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """For a hard login-gated host (Zhihu — no server-side reader can pass it)
    the error names the logged-in-browser skill to use instead."""
    monkeypatch.setattr(settings.web, "jina_api_key", SecretStr("k"))
    payload = _make_jina_response(
        title="\u5b89\u5168\u9a8c\u8bc1 - \u77e5\u4e4e",
        content="\u8bf7\u60a8\u767b\u5f55\u540e\u67e5\u770b\u66f4\u591a\u4e13\u4e1a\u4f18\u8d28\u5185\u5bb9\u3002",
    )
    with (
        patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)),
        pytest.raises(FetchError, match="bot-challenge or login wall"),
    ):
        ava.web.fetch([("https://zhuanlan.zhihu.com/p/123", "summarize")])
    assert "input" not in mock_llm


def test_fetch_long_article_mentioning_marker_not_flagged(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """A real article that merely mentions a wall phrase is not a wall — the
    length cap keeps it from a false positive, so it reaches the model."""
    monkeypatch.setattr(settings.web, "jina_api_key", SecretStr("k"))
    body = "An article about how Cloudflare shows 'Just a moment...' pages.\n" + ("x" * 5_000)
    payload = _make_jina_response(title="On bot challenges", content=body)
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        results = ava.web.fetch([("https://example.com/blog", "summarize")])
    assert results == ["FAKE ANSWER"]
    assert "Just a moment" in mock_llm["input"]


# ─── batch fetch (concurrency) ───


def test_fetch_batch_returns_results_in_input_order(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Multiple targets: results come back in the same order as the input list,
    regardless of which finishes first."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)

    call_order: list[str] = []
    # Use distinct URLs so we can verify order
    responses: dict[str, bytes] = {
        "https://a.com": _make_jina_response(title="A", content="content A"),
        "https://b.com": _make_jina_response(title="B", content="content B"),
        "https://c.com": _make_jina_response(title="C", content="content C"),
    }

    def _side_effect(req: Any, timeout: Any):
        url = req.full_url
        call_order.append(url)
        for key, payload in responses.items():
            if key in url:
                return _FakeResp(payload)
        return _FakeResp(_make_jina_response(content="unknown"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_side_effect):
        results = ava.web.fetch(
            [
                ("https://a.com", "summarize a"),
                ("https://b.com", "summarize b"),
                ("https://c.com", "summarize c"),
            ]
        )

    assert results == ["FAKE ANSWER", "FAKE ANSWER", "FAKE ANSWER"]
    assert len(call_order) == 3


def test_fetch_batch_runs_concurrently(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Batch fetch must run in parallel — the total wall-clock time should be
    close to one fetch's latency, not the sum of all three."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)

    barrier = threading.Barrier(3, timeout=5)

    def _slow_fetch(req, timeout):
        # All three threads hit the barrier, then proceed together
        barrier.wait()
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_slow_fetch):
        t0 = time.monotonic()
        results = ava.web.fetch(
            [
                ("https://a.com", "p1"),
                ("https://b.com", "p2"),
                ("https://c.com", "p3"),
            ]
        )
        elapsed = time.monotonic() - t0

    assert results == ["FAKE ANSWER", "FAKE ANSWER", "FAKE ANSWER"]
    # All three ran concurrently — total time should be roughly one fetch latency,
    # not 3x. The barrier ensures they all proceed at roughly the same time.
    assert elapsed < 3.0, f"Fetch took {elapsed:.1f}s — should be concurrent (< 3s)"


def test_fetch_signature_includes_effort_and_max_concurrent() -> None:
    """`effort` (None = keep settings.web.web_fetch_reasoning) plus an
    optional `max_concurrent` ceiling on in-flight targets — None (the default)
    keeps unbounded concurrency, and rate limiting stays with the provider
    (a 429 surfaces as FetchError)."""
    import inspect

    sig = inspect.signature(ava.web.fetch)
    assert list(sig.parameters) == ["targets", "max_chars", "effort", "max_concurrent"]
    assert sig.parameters["effort"].default is None
    assert sig.parameters["max_concurrent"].default is None


def test_fetch_batch_empty_list(monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]) -> None:
    """Empty targets list returns empty results list."""
    results = ava.web.fetch([])
    assert results == []


def test_fetch_max_concurrent_validation(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """max_concurrent must be a positive int or None — bad values fail fast
    before any fetch or model call."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    with pytest.raises(ValueError, match="at least 1"):
        ava.web.fetch([("https://example.com", "summarize")], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        ava.web.fetch([("https://example.com", "summarize")], max_concurrent=-3)
    with pytest.raises(TypeError, match="int or None"):
        ava.web.fetch(
            [("https://example.com", "summarize")],
            max_concurrent="2",  # type: ignore[arg-type]
        )


def test_fetch_max_concurrent_caps_inflight(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """max_concurrent=N keeps at most N fetches in flight — the observed peak
    never exceeds the ceiling, and results still come back in input order."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)

    lock = threading.Lock()
    inflight = 0
    peak = 0

    def _tracking_fetch(req, timeout):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        return _FakeResp(_make_jina_response(content="ok"))

    with patch("ava.web.urllib.request.urlopen", side_effect=_tracking_fetch):
        results = ava.web.fetch(
            [
                ("https://a.com", "p1"),
                ("https://b.com", "p2"),
                ("https://c.com", "p3"),
                ("https://d.com", "p4"),
                ("https://e.com", "p5"),
                ("https://f.com", "p6"),
            ],
            max_concurrent=2,
        )
    assert results == ["FAKE ANSWER"] * 6
    assert peak == 2, f"peak in-flight {peak} exceeded ceiling 2"


# ─── web-fetch model / reasoning config ───


def test_fetch_uses_configured_model_and_reasoning(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """When AVA_WEB_FETCH_MODEL and AVA_WEB_FETCH_REASONING are set, fetch
    uses them — not the understand text model."""
    monkeypatch.setattr(settings.web, "web_fetch_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings.web, "web_fetch_reasoning", "high")
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="ok")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")])
    assert mock_llm["model"] == "deepseek-v4-pro"
    assert mock_llm["reasoning_effort"] == "high"


def test_fetch_default_model_is_flash(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Default model is deepseek-v4-flash (cheap) and reasoning is none —
    independent of the agent's main model."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="ok")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")])
    assert mock_llm["model"] == "deepseek-v4-flash"
    assert mock_llm["reasoning_effort"] == "none"


def test_fetch_effort_overrides_settings_reasoning(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Per-call effort='high' wins over the settings default (none) — the wire
    reasoning_effort is 'high', not settings.web.web_fetch_reasoning."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="ok")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")], effort="high")
    assert mock_llm["reasoning_effort"] == "high"


def test_fetch_effort_none_keeps_settings_reasoning(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """effort=None (default) falls back to settings.web.web_fetch_reasoning —
    a cluster that configured 'high' keeps it, and a per-call 'low' still wins."""
    monkeypatch.setattr(settings.web, "web_fetch_reasoning", "high")
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="ok")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")])
    assert mock_llm["reasoning_effort"] == "high"

    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")], effort="low")
    assert mock_llm["reasoning_effort"] == "low"


def test_fetch_effort_accepts_enum_member(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """A ReasoningEffort member is accepted — same wire value as its literal."""
    from shared.lm._effort import ReasoningEffort

    monkeypatch.setattr(settings.web, "jina_api_key", None)
    payload = _make_jina_response(content="ok")
    with patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)):
        ava.web.fetch([("https://example.com", "summarize")], effort=ReasoningEffort.MAX)
    assert mock_llm["reasoning_effort"] == "max"


def test_fetch_effort_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """Unknown effort strings fail fast before any fetch or model call."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)
    with pytest.raises(ValueError, match="effort"):
        ava.web.fetch([("https://example.com", "summarize")], effort="ultra")
    with pytest.raises(TypeError, match="effort"):
        ava.web.fetch([("https://example.com", "summarize")], effort=3)  # type: ignore[arg-type]


def test_fetch_llm_build_error_wraps_as_fetcherror(
    monkeypatch: pytest.MonkeyPatch, mock_llm: dict[str, Any]
) -> None:
    """When build_chat_model raises RuntimeError (missing API key etc.),
    it surfaces as FetchError — the agent can diagnose the problem."""
    monkeypatch.setattr(settings.web, "jina_api_key", None)

    def _failing_build(
        model: str,
        *,
        thinking: object = None,
        reasoning_effort: object = None,
        streaming: object = None,
        timeout: float | None = None,
    ):
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _failing_build)
    payload = _make_jina_response(content="ok")
    with (
        patch("ava.web.urllib.request.urlopen", return_value=_FakeResp(payload)),
        pytest.raises(FetchError, match="DEEPSEEK_API_KEY"),
    ):
        ava.web.fetch([("https://example.com", "summarize")])
