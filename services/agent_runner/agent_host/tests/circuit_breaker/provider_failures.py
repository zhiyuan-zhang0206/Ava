"""Permanent provider failures and sufficient summaries for circuit-breaker tests."""

import httpx2
import openai

__all__ = ["LONG_SUMMARY", "FakeProviderStatusError"]

# A summary long enough to clear COMPACT_MIN_SUMMARY_CHARS.
LONG_SUMMARY = "## Requests\nfollow the template. " * 60


class FakeProviderStatusError(openai.APIStatusError):
    """An actual SDK status error with a synthetic response."""

    def __init__(self, status_code: int, body: object = None) -> None:
        response = httpx2.Response(
            status_code, request=httpx2.Request("POST", "https://audit.invalid")
        )
        super().__init__(f"HTTP {status_code}", response=response, body=body)
