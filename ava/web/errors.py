from __future__ import annotations

__all__ = ["FetchError", "SearchError", "WebError"]


class WebError(Exception):
    pass


class SearchError(WebError):
    pass


class FetchError(WebError):
    pass
