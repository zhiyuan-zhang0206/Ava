"""Shared HTTP-backend failure response for observability read endpoints."""

from typing import NoReturn

import httpx
from fastapi import HTTPException


def raise_backend_unavailable(exc: httpx.HTTPError, *, backend: str = "loki") -> NoReturn:
    """Raise the consistent retriable 503 for an unavailable read backend."""
    raise HTTPException(
        status_code=503,
        detail=f"{backend} backend unavailable ({type(exc).__name__}); retry in a moment",
        headers={"Retry-After": "1"},
    ) from exc
