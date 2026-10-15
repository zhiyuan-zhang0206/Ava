import httpx2
import openai

__all__ = ["LONG_SUMMARY", "FakeProviderStatusError"]

LONG_SUMMARY = "## Requests\nfollow the template. " * 60


class FakeProviderStatusError(openai.APIStatusError):
    def __init__(self, status_code: int, body: object = None) -> None:
        response = httpx2.Response(
            status_code, request=httpx2.Request("POST", "https://audit.invalid")
        )
        super().__init__(f"HTTP {status_code}", response=response, body=body)
