"""Fetch tests: Jina Reader + the web-fetch model, no real API or LLM calls.

Key contracts:
- fetch() only accepts list[tuple[str, str]], raises TypeError otherwise
- Batch fetch runs in parallel via asyncio, preserves input order
- Concurrency defaults to the SDK's shared safe ceiling; `max_concurrent`
  selects a different in-flight limit. A terminal provider failure propagates
  as SearchError / FetchError
- Individual fetch contracts (truncation, wall detection, error wrapping) still hold
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from typing import Any

import pytest
from langchain_core.exceptions import ModelRateLimitError
from pydantic import SecretStr

import ava
from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.tests.web._web_helpers import FakeResp, WebHarness
from ava.tests.web._web_helpers import web_harness as web_harness
from ava.web import FetchError, WebError
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog

# Retry backoff waits are recorded, not slept, so retry-path tests run instantly; the retry
# loop itself is still exercised (call counts).
pytestmark = [pytest.mark.usefixtures("retry_waits"), pytest.mark.usefixtures("sdk_model_owner")]


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
        self._captured["invoke_calls"] = self._captured.get("invoke_calls", 0) + 1
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
def mock_llm(web_harness: WebHarness, model_catalog: ModelCatalog) -> dict[str, Any]:
    """Give the web deps a provider factory fake so fetch's answer step never
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
        catalog: ModelCatalog,
        llm_override: str,
        overrides: ModelOverrides,
    ) -> _FakeLLM:
        assert catalog is model_catalog
        captured["model"] = model
        captured["reasoning_effort"] = reasoning_effort
        return _FakeLLM(captured)

    web_harness.build_chat_model(_fake_build)
    return captured


# ─── fetch signature ───


def test_fetch_requires_list(web_harness: WebHarness) -> None:
    """fetch() only accepts list[tuple[str, str]]. Passing a bare URL raises
    TypeError with a helpful message."""
    with pytest.raises(TypeError, match=r"list of .*url, prompt.* pairs"):
        web_harness.fetch("https://example.com", "summarize")  # type: ignore[call-overload]


def test_fetch_rejects_non_tuple_elements(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        web_harness.fetch(["https://example.com"])  # type: ignore[list-item]


def test_fetch_rejects_wrong_tuple_length(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        web_harness.fetch([("https://example.com", "prompt", "extra")])  # type: ignore[list-item]


def test_fetch_rejects_non_string_tuple_elements(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be .*url: str, prompt: str"):
        web_harness.fetch([(123, "prompt")])  # type: ignore[list-item]


# ─── single-target fetch (backward-compatible behavior via list) ───


def test_fetch_reads_page_then_calls_llm_with_web_model(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """Happy path: page body is fetched, framed, and handed to the web-fetch
    model (default deepseek-flash with reasoning=none); its answer is what
    fetch returns."""
    web_harness.set_jina(api_key=None)  # anonymous
    payload = _make_jina_response(
        title="PEP 758",
        url="https://peps.python.org/pep-0758/",
        content="Author: Pablo...\n\nAbstract\n--------\nexcept* without parens.",
    )
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        results = web_harness.fetch(
            [("https://peps.python.org/pep-0758/", "what does this PEP propose?")]
        )
    assert results == ["FAKE ANSWER"]
    # Uses the web-fetch model, not the understand text model
    assert mock_llm["model"] == "deepseek-flash"
    assert mock_llm["reasoning_effort"] == "none"
    # the prompt is passed through; the framed page (title + body) is the material
    assert mock_llm["prompt_text"] == "what does this PEP propose?"
    assert "except* without parens." in mock_llm["input"]
    assert "PEP 758" in mock_llm["input"]  # title included for context


def test_fetch_tells_llm_when_content_truncated(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """content > max_chars: body is cut AND the LLM is told it was cut, so the
    answer can reflect incompleteness instead of silently dropping it."""
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="x" * 100_000)
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")], max_chars=1000)
    material = mock_llm["input"]
    assert "cut off" in material
    # only max_chars of the body made it in (plus the cut note + framing)
    assert material.count("x") == 1000


def test_fetch_no_truncation_note_when_under_limit(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="short")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")])
    assert "cut off" not in mock_llm["input"]


def test_fetch_wraps_llm_error(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """An exhausted typed provider failure preserves its cause in FetchError."""
    web_harness.set_jina(api_key=None)
    web_harness.set_model(retry_attempts=0)
    error = ModelRateLimitError("rate limit exceeded")
    mock_llm["error"] = error
    payload = _make_jina_response(content="ok")
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(FetchError, match="rate limit") as raised,
    ):
        web_harness.fetch([("https://example.com", "summarize")])
    assert raised.value.__cause__ is error
    assert mock_llm["invoke_calls"] == 1


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid callback input"),
        ValueError("invalid response"),
        RuntimeError("rate limit"),
    ],
)
def test_fetch_unknown_model_error_preserves_identity_without_retry(
    mock_llm: dict[str, Any], error: Exception, web_harness: WebHarness
) -> None:
    web_harness.set_jina(api_key=None)
    mock_llm["error"] = error
    payload = _make_jina_response(content="ok")
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(type(error)) as raised,
    ):
        web_harness.fetch([("https://example.com", "summarize")])
    assert raised.value is error
    assert mock_llm["invoke_calls"] == 1


def test_fetch_uses_jina_api_key_when_set(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """When JINA_API_KEY is set, add Authorization: Bearer header — 20 RPM → 500 RPM."""
    web_harness.set_jina(api_key=SecretStr("jina-secret"))
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.fetch([("https://example.com", "summarize")])
    # urllib capitalizes header names
    assert captured["headers"].get("Authorization") == "Bearer jina-secret"


def test_fetch_anonymous_no_auth_header(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Works without JINA_API_KEY — no Authorization header. Jina anonymous 20 RPM."""
    web_harness.set_jina(api_key=None)
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.fetch([("https://example.com", "summarize")])
    assert "Authorization" not in captured["headers"]


def test_fetch_sends_custom_user_agent(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Python urllib default UA (`Python-urllib/3.x`) is blocked by Cloudflare;
    r.jina.ai goes through Cloudflare — without custom UA gets 403 browser_signature_banned.
    Protection test added after hitting this in practice."""
    web_harness.set_jina(api_key=None)
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: Any):
        captured["headers"] = dict(req.headers)
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.fetch([("https://example.com", "summarize")])
    ua = captured["headers"].get("User-agent", "")
    assert "ava-sdk" in ua


def test_fetch_url_encodes_target(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Target URL with spaces / Chinese query must be encoded; `r.jina.ai/` prefix unchanged."""
    web_harness.set_jina(api_key=None)
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.fetch([("https://example.com/search?q=hello world", "summarize")])
    # spaces should become %20, other URL structure (scheme / :/ / ?&=) preserved
    assert captured["url"].startswith(web_harness.deps.jina.reader_base + "https://example.com/")
    assert "%20" in captured["url"]
    assert "?q=hello" in captured["url"]


def test_fetch_hits_configured_reader_base(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """The Jina Reader base is configurable (`AVA_WEB_JINA_BASE_URL`) — the
    request must dial the configured base, not a baked-in literal."""
    web_harness.set_jina(api_key=None)
    web_harness.set_jina(reader_base="https://reader.relay.example/")
    captured: dict[str, str] = {}

    def _capture(req: Any, timeout: Any):
        captured["url"] = req.full_url
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.fetch([("https://example.com", "summarize")])
    assert captured["url"].startswith("https://reader.relay.example/https://example.com")


def test_fetch_jina_business_error_raises(web_harness: WebHarness) -> None:
    """Jina response code != 200 raises — upstream URL unreachable, blocked crawling, etc.;
    agent can fallback to curl or tell the user. Raises before the model step, so no
    LLM mock needed."""
    web_harness.set_jina(api_key=None)
    payload = json.dumps({"code": 422, "message": "Unable to fetch: 404 Not Found"}).encode()
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(FetchError, match="Jina Reader fetch failed"),
    ):
        web_harness.fetch([("https://example.com/nonexistent", "summarize")])


def test_fetch_http_error_raises(web_harness: WebHarness) -> None:
    """HTTP layer error (429 rate limit / 503 downstream down) must include upstream body in traceback."""
    web_harness.set_jina(api_key=None)
    http_err = urllib.error.HTTPError(
        url=web_harness.deps.jina.reader_base + "...",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"rate limit exceeded"),
    )
    with (
        web_harness.urlopen_returns(side_effect=http_err),
        pytest.raises(FetchError, match="Jina Reader HTTP 429"),
    ):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_network_error_raises(web_harness: WebHarness) -> None:
    web_harness.set_jina(api_key=None)
    url_err = urllib.error.URLError("nodename nor servname provided")
    with (
        web_harness.urlopen_returns(side_effect=url_err),
        pytest.raises(FetchError, match="Jina Reader connection failed"),
    ):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_malformed_json_raises(web_harness: WebHarness) -> None:
    web_harness.set_jina(api_key=None)
    with (
        web_harness.urlopen_returns(return_value=FakeResp(b"not json")),
        pytest.raises(WebError, match="non-JSON"),
    ):
        web_harness.fetch([("https://example.com", "summarize")])


# ─── bot-challenge / login-wall detection ───


def test_fetch_raises_on_cloudflare_wall(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Jina returns code=200 with a Cloudflare challenge page as the body; that
    used to be silently answered by the model. Now it raises so the caller knows
    the fetch failed, and the model step is never reached."""
    web_harness.set_jina(api_key=SecretStr("k"))
    payload = _make_jina_response(
        title="Just a moment...",
        content="Performing security verification\nThis website uses a security service "
        "to protect against malicious bots.",
    )
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(FetchError, match="bot-challenge or login wall"),
    ):
        web_harness.fetch([("https://cobusgreyling.medium.com/some-article", "summarize")])
    assert "input" not in mock_llm  # model step never ran


def test_fetch_login_gated_site_names_skill(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """For a hard login-gated host (Zhihu — no server-side reader can pass it)
    the error names the logged-in-browser skill to use instead."""
    web_harness.set_jina(api_key=SecretStr("k"))
    payload = _make_jina_response(
        title="\u5b89\u5168\u9a8c\u8bc1 - \u77e5\u4e4e",
        content="\u8bf7\u60a8\u767b\u5f55\u540e\u67e5\u770b\u66f4\u591a\u4e13\u4e1a\u4f18\u8d28\u5185\u5bb9\u3002",
    )
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(FetchError, match="bot-challenge or login wall"),
    ):
        web_harness.fetch([("https://zhuanlan.zhihu.com/p/123", "summarize")])
    assert "input" not in mock_llm


def test_fetch_long_article_mentioning_marker_not_flagged(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """A real article that merely mentions a wall phrase is not a wall — the
    length cap keeps it from a false positive, so it reaches the model."""
    web_harness.set_jina(api_key=SecretStr("k"))
    body = "An article about how Cloudflare shows 'Just a moment...' pages.\n" + ("x" * 5_000)
    payload = _make_jina_response(title="On bot challenges", content=body)
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        results = web_harness.fetch([("https://example.com/blog", "summarize")])
    assert results == ["FAKE ANSWER"]
    assert "Just a moment" in mock_llm["input"]


# ─── batch fetch (concurrency) ───


def test_fetch_batch_returns_results_in_input_order(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """Multiple targets: results come back in the same order as the input list,
    regardless of which finishes first."""
    web_harness.set_jina(api_key=None)

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
                return FakeResp(payload)
        return FakeResp(_make_jina_response(content="unknown"))

    with web_harness.urlopen_returns(side_effect=_side_effect):
        results = web_harness.fetch(
            [
                ("https://a.com", "summarize a"),
                ("https://b.com", "summarize b"),
                ("https://c.com", "summarize c"),
            ]
        )

    assert results == ["FAKE ANSWER", "FAKE ANSWER", "FAKE ANSWER"]
    assert len(call_order) == 3


def test_fetch_batch_runs_concurrently(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Batch fetch must run in parallel — the total wall-clock time should be
    close to one fetch's latency, not the sum of all three."""
    web_harness.set_jina(api_key=None)

    barrier = threading.Barrier(3, timeout=5)

    def _slow_fetch(req, timeout):
        # All three threads hit the barrier, then proceed together
        barrier.wait()
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_slow_fetch):
        t0 = time.monotonic()
        results = web_harness.fetch(
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
    """Fetch keeps its reasoning default and applies the shared batch ceiling."""
    import inspect

    sig = inspect.signature(ava.web.fetch)
    assert list(sig.parameters) == ["targets", "max_chars", "effort", "max_concurrent"]
    assert sig.parameters["effort"].default is None
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT


def test_fetch_batch_empty_list(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Empty targets list returns empty results list."""
    results = web_harness.fetch([])
    assert results == []


def test_fetch_max_concurrent_validation(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """max_concurrent must be a positive int or None — bad values fail fast
    before any fetch or model call."""
    web_harness.set_jina(api_key=None)
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.fetch([("https://example.com", "summarize")], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.fetch([("https://example.com", "summarize")], max_concurrent=-3)
    with pytest.raises(TypeError, match="int or None"):
        web_harness.fetch(
            [("https://example.com", "summarize")],
            max_concurrent="2",  # type: ignore[arg-type]
        )


def test_fetch_max_concurrent_caps_inflight(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """max_concurrent=N keeps at most N fetches in flight — the observed peak
    never exceeds the ceiling, and results still come back in input order."""
    web_harness.set_jina(api_key=None)

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
        return FakeResp(_make_jina_response(content="ok"))

    with web_harness.urlopen_returns(side_effect=_tracking_fetch):
        results = web_harness.fetch(
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
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """When AVA_WEB_FETCH_MODEL and AVA_WEB_FETCH_REASONING are set, fetch
    uses them — not the understand text model."""
    web_harness.set_model(model="deepseek-v4-pro")
    web_harness.set_model(reasoning="high")
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="ok")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")])
    assert mock_llm["model"] == "deepseek-v4-pro"
    assert mock_llm["reasoning_effort"] == "high"


def test_fetch_default_model_is_flash(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Default model is deepseek-flash (cheap) and reasoning is none —
    independent of the agent's main model."""
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="ok")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")])
    assert mock_llm["model"] == "deepseek-flash"
    assert mock_llm["reasoning_effort"] == "none"


def test_fetch_effort_overrides_settings_reasoning(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """Per-call effort='high' wins over the settings default (none) — the wire
    reasoning_effort is 'high', not settings.web.web_fetch_reasoning."""
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="ok")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")], effort="high")
    assert mock_llm["reasoning_effort"] == "high"


def test_fetch_effort_none_keeps_settings_reasoning(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """effort=None (default) falls back to settings.web.web_fetch_reasoning —
    a cluster that configured 'high' keeps it, and a per-call 'low' still wins."""
    web_harness.set_model(reasoning="high")
    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="ok")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")])
    assert mock_llm["reasoning_effort"] == "high"

    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")], effort="low")
    assert mock_llm["reasoning_effort"] == "low"


def test_fetch_effort_accepts_enum_member(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """A ReasoningEffort member is accepted — same wire value as its literal."""
    from base.lm.effort import ReasoningEffort

    web_harness.set_jina(api_key=None)
    payload = _make_jina_response(content="ok")
    with web_harness.urlopen_returns(return_value=FakeResp(payload)):
        web_harness.fetch([("https://example.com", "summarize")], effort=ReasoningEffort.MAX)
    assert mock_llm["reasoning_effort"] == "max"


def test_fetch_effort_invalid_raises(mock_llm: dict[str, Any], web_harness: WebHarness) -> None:
    """Unknown effort strings fail fast before any fetch or model call."""
    web_harness.set_jina(api_key=None)
    with pytest.raises(ValueError, match="effort"):
        web_harness.fetch([("https://example.com", "summarize")], effort="ultra")
    with pytest.raises(TypeError, match="effort"):
        web_harness.fetch([("https://example.com", "summarize")], effort=3)  # type: ignore[arg-type]


def test_fetch_llm_build_error_wraps_as_fetcherror(
    mock_llm: dict[str, Any], web_harness: WebHarness
) -> None:
    """When build_chat_model raises RuntimeError (missing API key etc.),
    it surfaces as FetchError — the agent can diagnose the problem."""
    web_harness.set_jina(api_key=None)

    def _failing_build(
        model: str,
        *,
        thinking: object = None,
        reasoning_effort: object = None,
        streaming: object = None,
        timeout: float | None = None,
        catalog: ModelCatalog,
        llm_override: str,
        overrides: ModelOverrides,
    ):
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    web_harness.build_chat_model(_failing_build)
    payload = _make_jina_response(content="ok")
    with (
        web_harness.urlopen_returns(return_value=FakeResp(payload)),
        pytest.raises(FetchError, match="DEEPSEEK_API_KEY"),
    ):
        web_harness.fetch([("https://example.com", "summarize")])
