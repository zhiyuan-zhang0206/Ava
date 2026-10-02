"""The browser-reach canary reports an unexpected failure as a skip, never as reachability success."""

import pytest

from services.healthchecks import browser_reach as hc


def test_canary_unexpected_failure_is_not_reachability_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_port: int, _url: str, _timeout_s: float) -> hc._CanaryResult:
        raise RuntimeError("CDP failed")

    monkeypatch.setattr(hc, "_canary_async", fail)
    assert hc.canary(9222, "https://example.invalid/", 1).outcome == "skip"
