"""Search tests: Brave Search API urllib thin wrapper, no real API calls, monkeypatch `urllib.request.urlopen`; split from ava/tests/test_web.py (task #4922)."""

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
from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.tests._web_helpers import _FakeResp
from ava.web import SearchError, WebError
from base.config import settings

# Retry backoff waits are recorded, not slept, so retry-path tests run instantly; the retry
# loop itself is still exercised (call counts).
pytestmark = pytest.mark.usefixtures("retry_waits")


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
        url=settings.web.web_brave_search_endpoint,
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
        url=settings.web.web_brave_search_endpoint,
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
        url=settings.web.web_brave_search_endpoint,
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


def test_endpoint_defaults_pin_before_literals() -> None:
    """The settings defaults must stay byte-identical to the URLs the module
    used to hard-code — a future default drift breaks the 'behavior unchanged'
    contract that the other endpoint tests assume (they reference the settings
    values, so they cannot catch it themselves)."""
    assert (
        settings.web.web_brave_search_endpoint == "https://api.search.brave.com/res/v1/web/search"
    )
    assert settings.web.web_jina_reader_base == "https://r.jina.ai/"


def test_search_hits_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Brave endpoint is configurable (`AVA_WEB_BRAVE_SEARCH_ENDPOINT`) —
    the request must dial the configured URL, not a baked-in literal."""
    monkeypatch.setattr(settings.web, "brave_api_key", SecretStr("fake-key"))
    monkeypatch.setattr(
        settings.web, "web_brave_search_endpoint", "https://search.relay.example/api"
    )
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return _FakeResp(
            _make_brave_response([{"title": "t", "url": "https://a.com", "description": "d"}])
        )

    with patch("ava.web.urllib.request.urlopen", side_effect=_capture):
        ava.web.search(["query"])
    assert captured["url"].startswith("https://search.relay.example/api?")


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
    """The public default applies the shared safe batch ceiling."""
    import inspect

    sig = inspect.signature(ava.web.search)
    assert list(sig.parameters) == ["queries", "count", "max_concurrent"]
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT


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
