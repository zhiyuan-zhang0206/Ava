"""Search tests: Brave Search API urllib thin wrapper, no real API calls, monkeypatch `urllib.request.urlopen`; split from meiva/tests/namespaces/web/test_web.py (task #4922)."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

import meiva
from base.config import settings
from base.config.service_read import ConfigAuthority
from meiva.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.sdk_surface.settings import config_authority
from meiva.tests.namespaces.web._web_helpers import FakeResp, WebHarness
from meiva.tests.namespaces.web._web_helpers import web_harness as web_harness
from meiva.web import SearchError, WebError, plans, runner, wiring

# Retry backoff waits are recorded, not slept, so retry-path tests run instantly; the retry
# loop itself is still exercised (call counts).
pytestmark = [pytest.mark.usefixtures("retry_waits"), pytest.mark.usefixtures("sdk_model_owner")]


def _make_brave_response(results: list[dict]) -> bytes:
    return json.dumps({"web": {"results": results}}).encode()


def test_raises_when_api_key_missing(web_harness: WebHarness) -> None:
    web_harness.set_brave(api_key=None)
    with pytest.raises(SearchError, match="BRAVE_API_KEY"):
        web_harness.search(["anything"])


def test_parses_brave_response_into_dataclass(web_harness: WebHarness) -> None:
    web_harness.set_brave(api_key=SecretStr("fake-key"))
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
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        results = web_harness.search(["python asyncio"])
    assert len(results) == 1
    assert len(results[0]) == 2
    assert isinstance(results[0][0], meiva.web.SearchResult)
    assert results[0][0].title == "Python asyncio docs"
    assert results[0][0].url == "https://docs.python.org/3/library/asyncio.html"
    assert results[0][0].snippet == "The asyncio library..."
    assert results[0][0].kind == "web"


def test_empty_results_returns_empty_list(web_harness: WebHarness) -> None:
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    payload = _make_brave_response([])
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        assert web_harness.search(["nonsense query qqqq"]) == [[]]


def test_missing_web_section_returns_empty_list(web_harness: WebHarness) -> None:
    """Brave response missing the `web` key — per API docs only `type` and `query`
    are guaranteed; `web` and other result-type keys appear only when relevant
    data is available. Missing `web` = no web results → return empty list."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    with web_harness.urlopen_returns(
        return_value=FakeResp(
            b'{"type": "search", "query": {"original": "q"}, "news": {"results": []}}'
        )
    ):
        results = web_harness.search(["query"])
    assert results == [[]]


def test_collects_results_from_all_sections(web_harness: WebHarness) -> None:
    """search() collects from web, news, videos — each tagged with kind."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    payload = json.dumps(
        {
            "web": {"results": [{"title": "W", "url": "https://a.com", "description": "d"}]},
            "news": {"results": [{"title": "N", "url": "https://b.com", "description": "d"}]},
            "videos": {"results": [{"title": "V", "url": "https://c.com", "description": "d"}]},
        }
    ).encode()
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        results = web_harness.search(["query"])
    assert len(results) == 1
    assert len(results[0]) == 3
    assert {r.kind for r in results[0]} == {"web", "news", "videos"}
    assert results[0][0].kind == "web"
    assert results[0][1].kind == "news"
    assert results[0][2].kind == "videos"


def test_http_error_raises_with_detail(web_harness: WebHarness) -> None:
    """API returns 422/429/500 etc., must include upstream body in traceback — agent can
    diagnose (rate limit / invalid param / expired key)."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url=settings.web.web_brave_search_endpoint,
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    with (
        web_harness.urlopen_returns(side_effect=http_err),
        pytest.raises(SearchError, match="Brave Search HTTP 429"),
    ):
        web_harness.search(["query"])


def test_network_error_raises(web_harness: WebHarness) -> None:
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    url_err = urllib.error.URLError("nodename nor servname provided")
    with (
        web_harness.urlopen_returns(side_effect=url_err),
        pytest.raises(WebError, match="connection failed"),
    ):
        web_harness.search(["query"])


def test_transient_429_retries_then_succeeds(web_harness: WebHarness) -> None:
    """A transient 429 is retried (R2-D): the search succeeds once the
    upstream recovers — pre-R2 a 429 failed the whole search."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url=settings.web.web_brave_search_endpoint,
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    calls = {"n": 0}

    def _flaky(*_args: object, **_kwargs: object) -> FakeResp:
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_err
        return FakeResp(
            _make_brave_response([{"title": "t", "url": "https://a.com", "description": "d"}])
        )

    with web_harness.urlopen_returns(side_effect=_flaky):
        results = web_harness.search(["query"])
    assert calls["n"] == 3
    assert results[0][0].title == "t"


def test_persistent_429_exhausts_retries(web_harness: WebHarness) -> None:
    """A persistent 429 exhausts the budget and surfaces as SearchError —
    never silently swallowed (R2-D D3: idempotent reads fail loud)."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    http_err = urllib.error.HTTPError(
        url=settings.web.web_brave_search_endpoint,
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "rate limit exceeded"}'),
    )
    calls = {"n": 0}

    def _always(*_args: object, **_kwargs: object) -> FakeResp:
        calls["n"] += 1
        raise http_err

    with (
        web_harness.urlopen_returns(side_effect=_always),
        pytest.raises(SearchError, match="Brave Search HTTP 429"),
    ):
        web_harness.search(["query"])
    assert calls["n"] == 3  # max_attempts


def test_malformed_json_raises(web_harness: WebHarness) -> None:
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    with (
        web_harness.urlopen_returns(return_value=FakeResp(b"not json at all")),
        pytest.raises(WebError, match="non-JSON"),
    ):
        web_harness.search(["query"])


def test_count_clamped_to_brave_max(web_harness: WebHarness) -> None:
    """count > 20 would be rejected by Brave with 422; we clamp to 20 beforehand."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["x"], count=100)
    assert "count=20" in captured["url"]


def test_count_floor_at_one(web_harness: WebHarness) -> None:
    """count <= 0 clamped to 1 — Brave returns 422 for count=0."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["x"], count=0)
    assert "count=1" in captured["url"]


def test_subscription_token_header_sent(web_harness: WebHarness) -> None:
    """Brave authentication uses `X-Subscription-Token` header — missing it gets 401, must verify."""
    web_harness.set_brave(api_key=SecretStr("my-secret-key"))
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["x"])
    # urllib capitalizes header names; Brave accepts `X-Subscription-Token`
    assert captured["headers"].get("X-subscription-token") == "my-secret-key"


def test_search_result_str_format() -> None:
    """The format of `print(r)` for later LLM context when concatenating multiple results — kind prefix + 3-line block stable."""
    r = meiva.web.SearchResult(title="Foo", url="https://example.com", snippet="A foo page.")
    assert str(r) == "[web] Foo\n  https://example.com\n  A foo page."


def test_endpoint_defaults_pin_before_literals(web_harness: WebHarness) -> None:
    """The settings defaults must stay byte-identical to the URLs the module
    used to hard-code — a future default drift breaks the 'behavior unchanged'
    contract that the other endpoint tests assume (they reference the settings
    values, so they cannot catch it themselves)."""
    assert (
        settings.web.web_brave_search_endpoint == "https://api.search.brave.com/res/v1/web/search"
    )
    assert web_harness.fetch_deps.jina.reader_base == "https://r.jina.ai/"


def test_search_hits_configured_endpoint(web_harness: WebHarness) -> None:
    """The Brave endpoint is configurable (`MEIVA_WEB_BRAVE_SEARCH_ENDPOINT`) —
    the request must dial the configured URL, not a baked-in literal."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    web_harness.set_brave(endpoint="https://search.relay.example/api")
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return FakeResp(
            _make_brave_response([{"title": "t", "url": "https://a.com", "description": "d"}])
        )

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["query"])
    assert captured["url"].startswith("https://search.relay.example/api?")


def test_search_uses_two_owned_boots_and_live_updates(tmp_path: Path) -> None:
    """The deps follow the configuration owner they are built from, including later owner writes."""
    import os

    from base.config import ConfigBoot
    from base.config.service_read import ConfigAuthority

    observed: list[tuple[str, str, float]] = []

    def capture(req: Any, timeout: float) -> FakeResp:
        observed.append((req.full_url, req.get_header("X-subscription-token"), timeout))
        return FakeResp(_make_brave_response([]))

    def search(authority: ConfigAuthority) -> list[list[meiva.web.SearchResult]]:
        deps = replace(wiring.search_deps_for(authority), open_url=capture)
        return runner.run_search(plans.plan_search(["q"], 20, None), deps, meiva.web.SearchResult)

    try:
        with patch.dict(os.environ):
            first, second = ConfigBoot(), ConfigBoot()
            authorities: list[ConfigAuthority] = []
            for index, boot in enumerate((first, second), start=1):
                boot.set_field("brave_api_key", SecretStr(f"key-{index}"))
                boot.set_field("web_brave_search_endpoint", f"https://search-{index}.example/api")
                boot.set_field("web_search_timeout_seconds", float(index + 10))
                boot.set_field("web_max_search_results", index)
                authorities.append(
                    ConfigAuthority.deferred(
                        runtime=boot.view,
                        build_all_domains=boot.ensure_eager,
                        env_path=tmp_path / f"config-{index}.env",
                    )
                )
                assert not boot.is_full()
            for authority in authorities:
                assert search(authority) == [[]]
            first.set_field("brave_api_key", SecretStr("key-updated"))
            first.set_field("web_search_timeout_seconds", 31.0)
            assert search(authorities[0]) == [[]]
            assert observed == [
                ("https://search-1.example/api?q=q&count=1", "key-1", 11.0),
                ("https://search-2.example/api?q=q&count=2", "key-2", 12.0),
                ("https://search-1.example/api?q=q&count=1", "key-updated", 31.0),
            ]
    finally:
        time.tzset()


@dataclass(frozen=True, slots=True)
class _RecordingAuthority(ConfigAuthority):
    """A configuration owner that records which fields were read from it."""

    reads: list[str] = field(default_factory=list[str])

    def service_field_value(self, name: str) -> Any:
        self.reads.append(name)
        return ConfigAuthority.service_field_value(self, name)


def test_search_deps_read_only_brave_fields(web_harness: WebHarness) -> None:
    base = config_authority()
    authority = _RecordingAuthority(base.runtime, base.all_domains, base.env_path)
    wiring.search_deps_for(authority)
    assert sorted(authority.reads) == [
        "brave_api_key",
        "web_brave_search_endpoint",
        "web_max_search_results",
        "web_search_timeout_seconds",
    ]


def test_fetch_deps_read_no_brave_fields(web_harness: WebHarness) -> None:
    base = config_authority()
    authority = _RecordingAuthority(base.runtime, base.all_domains, base.env_path)
    model = web_harness.fetch_deps.fetch_model
    wiring.fetch_deps_for(authority, catalog=model.catalog, overrides=model.overrides)
    assert sorted(authority.reads) == sorted(set(authority.reads))
    assert not {
        "brave_api_key",
        "web_brave_search_endpoint",
        "web_search_timeout_seconds",
        "web_max_search_results",
    } & set(authority.reads)
    assert {"jina_api_key", "web_fetch_model"} <= set(authority.reads)


def test_search_reads_the_installation_it_is_called_under(web_harness: WebHarness) -> None:
    """`meiva.web.search` builds its deps from the SDK installation of the process."""
    assert wiring.search_deps().brave == web_harness.search_deps.brave


# ─── search signature ───


def test_search_requires_list(web_harness: WebHarness) -> None:
    """search() only accepts list[str]. Passing a bare string raises TypeError."""
    with pytest.raises(TypeError, match=r"list of query strings"):
        web_harness.search("not a list")  # type: ignore[arg-type]


def test_search_rejects_non_string_elements(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be a str"):
        web_harness.search(["ok", 123])  # type: ignore[list-item]


# ─── batch search (concurrency) ───


def test_search_batch_returns_results_in_input_order(web_harness: WebHarness) -> None:
    """Multiple queries: results come back in the same order as the input list."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))

    call_order: list[str] = []

    def _side_effect(req: Any, timeout: Any):
        url = req.full_url
        call_order.append(url)
        if "q=a" in url:
            return FakeResp(
                _make_brave_response([{"title": "A", "url": "https://a.com", "description": "d"}])
            )
        if "q=b" in url:
            return FakeResp(
                _make_brave_response([{"title": "B", "url": "https://b.com", "description": "d"}])
            )
        if "q=c" in url:
            return FakeResp(
                _make_brave_response([{"title": "C", "url": "https://c.com", "description": "d"}])
            )
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_side_effect):
        results = web_harness.search(["a", "b", "c"])

    assert len(results) == 3
    assert results[0][0].title == "A"
    assert results[1][0].title == "B"
    assert results[2][0].title == "C"
    assert len(call_order) == 3


def test_search_batch_runs_concurrently(web_harness: WebHarness) -> None:
    """Batch search must run in parallel — total time close to one search latency."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))

    barrier = threading.Barrier(3, timeout=5)

    def _slow_search(req, timeout):
        barrier.wait()
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_slow_search):
        t0 = time.monotonic()
        results = web_harness.search(["q1", "q2", "q3"])
        elapsed = time.monotonic() - t0

    assert results == [[], [], []]
    assert elapsed < 3.0, f"Search took {elapsed:.1f}s — should be concurrent (< 3s)"


def test_search_signature_includes_max_concurrent() -> None:
    """The public default applies the shared safe batch ceiling."""
    import inspect

    sig = inspect.signature(meiva.web.search)
    assert list(sig.parameters) == ["queries", "count", "max_concurrent"]
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT


def test_search_batch_empty_list(web_harness: WebHarness) -> None:
    """Empty queries list returns empty results list."""
    results = web_harness.search([])
    assert results == []


def test_search_max_concurrent_validation(web_harness: WebHarness) -> None:
    """max_concurrent must be a positive int or None — bad values fail fast
    before any search call."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.search(["q"], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.search(["q"], max_concurrent=-3)
    with pytest.raises(TypeError, match="int or None"):
        web_harness.search(["q"], max_concurrent=2.5)  # type: ignore[arg-type]


def test_search_max_concurrent_caps_inflight(web_harness: WebHarness) -> None:
    """max_concurrent=N keeps at most N searches in flight — the observed peak
    never exceeds the ceiling, and results still come back in input order."""
    web_harness.set_brave(api_key=SecretStr("fake-key"))

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
        return FakeResp(_make_brave_response([]))

    with web_harness.urlopen_returns(side_effect=_tracking_search):
        results = web_harness.search(["q1", "q2", "q3", "q4", "q5", "q6"], max_concurrent=2)
    assert results == [[], [], [], [], [], []]
    assert peak == 2, f"peak in-flight {peak} exceeded ceiling 2"
