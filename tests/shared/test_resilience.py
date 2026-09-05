"""shared/resilience.py — R2-D retry primitives tests.

Covers the four entity contracts: Policy parameterization, the one retry-loop
implementation (retry/aretry), the one classification semantics
(http_classifier + compositional with_ overrides), and the shared jitter /
Retry-After helpers (design-concept.md §4.4, evaluation-record #14).
"""

from __future__ import annotations

import email.message
import io
import urllib.error

import httpx
import pytest

from shared.resilience import (
    ExponentialBackoff,
    Policy,
    aretry,
    extract_retry_after,
    http_classifier,
    jittered,
    retry,
)


class _Flaky:
    """Raises ``error`` for the first ``failures`` calls, then returns ``value``."""

    def __init__(self, error: Exception, failures: int, value: object = "ok") -> None:
        self._error = error
        self._remaining = failures
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self.value


def _http_error(status: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://example.test/x",
        code=status,
        msg=f"status {status}",
        hdrs=hdrs if retry_after else None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"{}"),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize backoff sleeps; assertions use call counts instead."""

    async def _no_asleep(_s: float) -> None:
        pass

    monkeypatch.setattr("shared.resilience._sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.resilience._asleep", _no_asleep)


class TestRetry:
    def test_success_first_attempt(self) -> None:
        f = _Flaky(ValueError("x"), 0)
        assert retry(Policy())(f) == "ok"
        assert f.calls == 1

    def test_transient_then_success(self) -> None:
        f = _Flaky(urllib.error.URLError("boom"), 2)
        assert retry(Policy(max_attempts=4))(f) == "ok"
        assert f.calls == 3

    def test_exhausts_raises_last_error(self) -> None:
        f = _Flaky(urllib.error.URLError("boom"), 100)
        with pytest.raises(urllib.error.URLError):
            retry(Policy(max_attempts=3))(f)
        assert f.calls == 3

    def test_permanent_status_not_retried(self) -> None:
        f = _Flaky(_http_error(400), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(max_attempts=3))(f)
        assert f.calls == 1

    def test_transient_status_retried(self) -> None:
        f = _Flaky(_http_error(429), 2)
        assert retry(Policy(max_attempts=4))(f) == "ok"
        assert f.calls == 3

    def test_non_http_exception_is_permanent(self) -> None:
        f = _Flaky(ValueError("bug"), 100)
        with pytest.raises(ValueError):
            retry(Policy(max_attempts=3))(f)
        assert f.calls == 1

    def test_idempotent_false_single_attempt(self) -> None:
        """Non-idempotent: exactly one execution even for retryable errors."""
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(idempotent=False, on_final_failure=lambda _e: None))(f)
        assert f.calls == 1

    def test_on_final_failure_hook_sees_last_error(self) -> None:
        seen: list[Exception] = []
        f = _Flaky(_http_error(500), 100)

        def _hook(exc: Exception) -> None:
            seen.append(exc)

        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(max_attempts=2, on_final_failure=_hook))(f)
        assert len(seen) == 1
        assert isinstance(seen[0], urllib.error.HTTPError)

    def test_on_final_failure_can_convert_error_type(self) -> None:
        def _hook(exc: Exception) -> None:
            raise RuntimeError(f"converted: {exc}") from exc

        f = _Flaky(_http_error(502), 100)
        with pytest.raises(RuntimeError, match="converted"):
            retry(Policy(on_final_failure=_hook))(f)

    def test_backoff_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exponential shape: base * factor**attempt, capped."""
        sleeps: list[float] = []
        monkeypatch.setattr("shared.resilience._sleep", sleeps.append)
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=4,
                    backoff=ExponentialBackoff(1.0, 2.0, 8.0),
                    jitter="none",
                )
            )(f)
        assert sleeps == [1.0, 2.0, 4.0]

    def test_backoff_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("shared.resilience._sleep", sleeps.append)
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=5,
                    backoff=ExponentialBackoff(1.0, 2.0, 3.0),
                    jitter="none",
                )
            )(f)
        assert sleeps == [1.0, 2.0, 3.0, 3.0]

    def test_retry_after_overrides_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("shared.resilience._sleep", sleeps.append)
        f = _Flaky(_http_error(429, retry_after="30"), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=2,
                    backoff=ExponentialBackoff(1.0, 1.0, 1.0),
                    jitter="none",
                )
            )(f)
        assert sleeps == [30.0]

    def test_respect_retry_after_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("shared.resilience._sleep", sleeps.append)
        f = _Flaky(_http_error(429, retry_after="30"), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=2,
                    backoff=ExponentialBackoff(1.0, 1.0, 1.0),
                    jitter="none",
                    respect_retry_after=False,
                )
            )(f)
        assert sleeps == [1.0]


class TestAretry:
    async def test_aretry_transient_then_success(self) -> None:
        f = _Flaky(httpx.ConnectError("boom"), 2)

        async def _call() -> object:
            return f()

        assert await aretry(Policy(max_attempts=4))(_call) == "ok"
        assert f.calls == 3

    async def test_aretry_permanent_not_retried(self) -> None:
        f = _Flaky(ValueError("bug"), 100)

        async def _call() -> object:
            return f()

        with pytest.raises(ValueError):
            await aretry(Policy(max_attempts=3))(_call)
        assert f.calls == 1

    async def test_aretry_idempotent_false_single_attempt(self) -> None:
        f = _Flaky(_http_error(503), 100)

        async def _call() -> object:
            return f()

        with pytest.raises(urllib.error.HTTPError):
            await aretry(Policy(idempotent=False, on_final_failure=lambda _e: None))(_call)
        assert f.calls == 1


class TestHttpClassifier:
    def test_transient_statuses(self) -> None:
        for code in (429, 500, 502, 503, 504):
            assert http_classifier(_http_error(code)) is True

    def test_permanent_statuses(self) -> None:
        for code in (400, 401, 403, 404, 422):
            assert http_classifier(_http_error(code)) is False

    def test_httpx_status_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "https://e/x"),
            response=httpx.Response(503),
        )
        assert http_classifier(exc) is True
        exc400 = httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "https://e/x"),
            response=httpx.Response(400),
        )
        assert http_classifier(exc400) is False

    def test_transport_errors_retryable(self) -> None:
        assert http_classifier(urllib.error.URLError("dns")) is True
        assert http_classifier(httpx.ConnectError("refused")) is True
        assert http_classifier(httpx.ReadTimeout("slow")) is True

    def test_other_exceptions_permanent(self) -> None:
        assert http_classifier(ValueError("bug")) is False
        assert http_classifier(KeyError("k")) is False

    def test_with_permanent_override(self) -> None:
        c = http_classifier.with_(permanent={429})
        assert c(_http_error(429)) is False  # forced permanent
        assert c(_http_error(500)) is True  # base semantics kept
        assert c(_http_error(400)) is False

    def test_with_transient_override(self) -> None:
        c = http_classifier.with_(transient={404})
        assert c(_http_error(404)) is True  # forced transient
        assert c(_http_error(429)) is True  # base semantics kept
        assert c(_http_error(400)) is False

    def test_with_overrides_accumulate(self) -> None:
        c = http_classifier.with_(permanent={429}).with_(transient={404})
        assert c(_http_error(429)) is False
        assert c(_http_error(404)) is True


class TestJitter:
    def test_none_mode_returns_delay(self) -> None:
        assert jittered(2.0, mode="none") == 2.0

    def test_agent_mode_bounds(self) -> None:
        # delay + phase in [0, span) + uniform(-span, span)
        for _ in range(100):
            j = jittered(2.0, span=1.0, mode="agent")
            assert 1.0 <= j < 4.0

    def test_random_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(2.0, span=1.0, mode="random")
            assert 1.0 <= j <= 3.0

    def test_never_negative_when_delay_below_span(self) -> None:
        """`delay < span` must not produce a negative sleep — time.sleep(-x)
        raises ValueError and masks the original exception on the retry path
        (audit 2026-08-08 P2; invoke_text's delay comes from a writable
        cluster setting, so this is production-reachable)."""
        for _ in range(200):
            assert jittered(0.5, span=1.0, mode="agent") >= 0.0
            assert jittered(0.5, span=1.0, mode="random") >= 0.0
        assert jittered(0.0, span=5.0, mode="random") >= 0.0


class TestExtractRetryAfter:
    def test_response_headers(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "5"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) == 5.0

    def test_retry_after_ms(self) -> None:
        resp = httpx.Response(429, headers={"retry-after-ms": "1500"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) == 1.5

    def test_urllib_headers_direct(self) -> None:
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "42"
        exc = urllib.error.HTTPError(
            url="https://e/x", code=429, msg="r", hdrs=hdrs, fp=io.BytesIO(b"{}")
        )
        assert extract_retry_after(exc) == 42.0

    def test_over_cap_ignored(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "3600"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) is None

    def test_no_headers(self) -> None:
        assert extract_retry_after(ValueError("x")) is None
        assert extract_retry_after(_http_error(429)) is None  # hdrs=None
