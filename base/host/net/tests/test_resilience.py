"""base/host/net/resilience.py — R2-D retry primitives tests.

Covers the four entity contracts: Policy parameterization, the one retry-loop
implementation (retry/aretry), the one classification semantics
(http_classifier + compositional with_ overrides), and the shared jitter /
Retry-After helpers (design-concept.md §4.4, evaluation-record #14).
"""

from __future__ import annotations

import asyncio
import email.message
import inspect
import io
import time
import urllib.error
from dataclasses import FrozenInstanceError

import httpx
import pytest

from base.host.net import resilience
from base.host.net.resilience import (
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


# Backoff waits are recorded, never slept; tests that do not read them assert call counts.
pytestmark = pytest.mark.usefixtures("retry_waits")

# Captured at import, before any fixture replaces them.
_DEFAULT_WAIT_HOOKS = (resilience.retry_sleep, resilience.retry_asleep)


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

    def test_backoff_sequence(self, retry_waits: list[float]) -> None:
        """Exponential shape: base * factor**attempt, capped."""
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=4,
                    backoff=ExponentialBackoff(1.0, 2.0, 8.0),
                    jitter="none",
                )
            )(f)
        assert retry_waits == [1.0, 2.0, 4.0]

    def test_backoff_capped(self, retry_waits: list[float]) -> None:
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=5,
                    backoff=ExponentialBackoff(1.0, 2.0, 3.0),
                    jitter="none",
                )
            )(f)
        assert retry_waits == [1.0, 2.0, 3.0, 3.0]

    def test_retry_after_overrides_backoff(self, retry_waits: list[float]) -> None:
        f = _Flaky(_http_error(429, retry_after="30"), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=2,
                    backoff=ExponentialBackoff(1.0, 1.0, 1.0),
                    jitter="none",
                )
            )(f)
        assert retry_waits == [30.0]

    def test_respect_retry_after_false(self, retry_waits: list[float]) -> None:
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
        assert retry_waits == [1.0]


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


class TestWaitHooks:
    """`retry_sleep` / `retry_asleep` are the public seam tests observe backoff waits through."""

    def test_hooks_are_public_and_default_to_the_real_waits(self) -> None:
        assert {"retry_sleep", "retry_asleep"} <= set(resilience.__all__)
        assert (time.sleep, asyncio.sleep) == _DEFAULT_WAIT_HOOKS

    def test_retry_reads_the_hook_at_each_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hook replaced after the retry was built still takes effect."""
        run = retry(
            Policy(max_attempts=3, backoff=ExponentialBackoff(1.0, 2.0, 8.0), jitter="none")
        )
        seen: list[float] = []
        monkeypatch.setattr(resilience, "retry_sleep", seen.append)
        with pytest.raises(urllib.error.HTTPError):
            run(_Flaky(_http_error(503), 100))
        assert seen == [1.0, 2.0]

    async def test_aretry_reads_the_hook_at_each_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = _Flaky(_http_error(503), 100)

        async def _call() -> object:
            return f()

        run = aretry(
            Policy(max_attempts=3, backoff=ExponentialBackoff(1.0, 2.0, 8.0), jitter="none")
        )
        seen: list[float] = []

        async def _record(seconds: float) -> None:
            seen.append(seconds)

        monkeypatch.setattr(resilience, "retry_asleep", _record)
        with pytest.raises(urllib.error.HTTPError):
            await run(_call)
        assert seen == [1.0, 2.0]

    async def test_retry_waits_records_sync_and_async_waits_in_call_order(
        self, retry_waits: list[float]
    ) -> None:
        policy = Policy(max_attempts=2, backoff=ExponentialBackoff(1.0, 1.0, 1.0), jitter="none")
        flaky_sync = _Flaky(urllib.error.URLError("boom"), 1)
        assert retry(policy)(flaky_sync) == "ok"
        flaky_async = _Flaky(urllib.error.URLError("boom"), 1)

        async def _call() -> object:
            return flaky_async()

        assert (
            await aretry(
                Policy(max_attempts=2, backoff=ExponentialBackoff(3.0, 1.0, 3.0), jitter="none")
            )(_call)
            == "ok"
        )
        assert retry_waits == [1.0, 3.0]
        assert inspect.iscoroutinefunction(resilience.retry_asleep)

    def test_retry_waits_fails_fast_when_a_loop_never_ends(self, retry_waits: list[float]) -> None:
        """A recorder that keeps filling means a caller loops around an instant wait (#1001)."""
        with pytest.raises(AssertionError, match="issue #1001"):
            for _ in range(100_000):
                resilience.retry_sleep(0.0)
        assert 0 < len(retry_waits) < 100_000


class TestHttpClassifier:
    def test_override_inputs_are_snapshots(self) -> None:
        permanent = {429}
        transient = {404}
        classifier = resilience._HttpClassifier(permanent=permanent, transient=transient)
        permanent.clear()
        permanent.add(500)
        transient.clear()
        transient.add(400)
        assert classifier(_http_error(429)) is False
        assert classifier(_http_error(500)) is True
        assert classifier(_http_error(404)) is True
        assert classifier(_http_error(400)) is False

    def test_classifier_fields_are_immutable(self) -> None:
        classifier = http_classifier.with_(transient={404})
        with pytest.raises(FrozenInstanceError):
            classifier.__setattr__("_permanent", frozenset({500}))
        assert classifier(_http_error(500)) is True

    def test_composition_preserves_the_original(self) -> None:
        original = http_classifier.with_(transient={404})
        extended = original.with_(permanent={429})
        assert original(_http_error(429)) is True
        assert extended(_http_error(429)) is False
        assert original(_http_error(404)) is True
        assert extended(_http_error(404)) is True

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
    @staticmethod
    def _fixed_phase(_span: float) -> float:
        return 1.25

    def test_none_mode_returns_delay(self) -> None:
        assert jittered(2.0, mode="none") == 2.0

    @pytest.mark.parametrize("random_value", [-100.0, 100.0])
    def test_phase_mode_adds_deterministic_phase_only(
        self, monkeypatch: pytest.MonkeyPatch, random_value: float
    ) -> None:
        def _uniform(_low: float, _high: float) -> float:
            return random_value

        monkeypatch.setattr("base.host.net.resilience._agent_phase", self._fixed_phase)
        monkeypatch.setattr("base.host.net.resilience.random.uniform", _uniform)
        assert jittered(2.0, span=5.0, mode="phase") == 3.25

    def test_phase_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(2.0, span=5.0, mode="phase")
            assert 2.0 <= j < 7.0

    @pytest.mark.parametrize("random_value, expected", [(-0.5, 2.0), (0.0, 4.0), (0.5, 6.0)])
    def test_relative_mode_scales_delay(
        self, monkeypatch: pytest.MonkeyPatch, random_value: float, expected: float
    ) -> None:
        def _uniform(low: float, high: float) -> float:
            assert (low, high) == (-0.5, 0.5)
            return random_value

        monkeypatch.setattr("base.host.net.resilience.random.uniform", _uniform)
        assert jittered(4.0, span=0.5, mode="relative") == expected

    def test_relative_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(4.0, span=0.5, mode="relative")
            assert 2.0 <= j <= 6.0

    def test_relative_mode_never_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _uniform(_low: float, _high: float) -> float:
            return -2.0

        monkeypatch.setattr("base.host.net.resilience.random.uniform", _uniform)
        assert jittered(0.5, span=2.0, mode="relative") == 0.0

    def test_relative_zero_span_returns_delay(self) -> None:
        assert jittered(4.0, span=0.0, mode="relative") == 4.0

    def test_retry_passes_phase_jitter_to_sleep(
        self, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
    ) -> None:
        def _uniform(_low: float, _high: float) -> float:
            return 100.0

        monkeypatch.setattr("base.host.net.resilience._agent_phase", self._fixed_phase)
        monkeypatch.setattr("base.host.net.resilience.random.uniform", _uniform)
        f = _Flaky(urllib.error.URLError("boom"), 1)
        assert retry(Policy(max_attempts=2, jitter="phase", jitter_span=5.0))(f) == "ok"
        assert retry_waits == [2.25]

    def test_retry_passes_relative_jitter_to_sleep(
        self, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
    ) -> None:
        def _unexpected_phase(_span: float) -> float:
            pytest.fail("unexpected phase")

        def _uniform(_low: float, _high: float) -> float:
            return 0.5

        monkeypatch.setattr("base.host.net.resilience._agent_phase", _unexpected_phase)
        monkeypatch.setattr("base.host.net.resilience.random.uniform", _uniform)
        f = _Flaky(urllib.error.URLError("boom"), 1)
        assert retry(Policy(max_attempts=2, jitter="relative", jitter_span=0.5))(f) == "ok"
        assert retry_waits == [1.5]

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
