"""The error `meiva.understand` raises."""

from __future__ import annotations

__all__ = ["UnderstandError"]


class UnderstandError(Exception):
    """`meiva.understand` failure — API key not set, a file that is not valid
    UTF-8 text, media over the 20MB inline cap, typed provider call failure, or an
    empty (safety-blocked) response. Catch it as
    `meiva.understand.UnderstandError`. Filesystem errors (missing path,
    permissions) and unknown model-invocation errors retain their original
    exception types; unknown errors are not retried."""
