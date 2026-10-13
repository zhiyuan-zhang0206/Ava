"""The results and errors of `ava.web`, shared by the facade and its implementation."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FetchError", "SearchError", "SearchResult", "WebError"]


class WebError(Exception):
    pass


class SearchError(WebError):
    pass


class FetchError(WebError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    kind: str = "web"

    def __str__(self) -> str:
        return f"[{self.kind}] {self.title}\n  {self.url}\n  {self.snippet}"
