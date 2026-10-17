"""Validate the arguments of `meiva.web.search` and `meiva.web.fetch`, with no ambient reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from base.lm.effort import ReasoningEffort, coerce_effort
from meiva.sdk_surface.batch import validate_max_concurrent
from meiva.sdk_surface.validation import coerce_str, coerce_typed

__all__ = ["FetchPlan", "SearchPlan", "plan_fetch", "plan_search"]


@dataclass(frozen=True)
class SearchPlan:
    """The queries of one `meiva.web.search` call, the result cap per query and the concurrency."""

    queries: list[str]
    count: int
    max_concurrent: int | None


@dataclass(frozen=True)
class FetchPlan:
    """The `(url, prompt)` pairs of one `meiva.web.fetch` call and how each is answered."""

    targets: list[tuple[str, str]]
    max_chars: int
    effort: ReasoningEffort | None
    max_concurrent: int | None


def plan_search(queries: list[str], count: int, max_concurrent: int | None) -> SearchPlan:
    """Check the queries and coerce `count`; raise on the first invalid input."""
    if not isinstance(queries, list):
        raise TypeError(
            f"search() takes a list of query strings, got {type(queries).__name__}. "
            f"Example: meiva.web.search(['python asyncio tutorial'])"
        )
    for i, q in enumerate(queries):
        if not isinstance(q, str):
            raise TypeError(f"queries[{i}] must be a str, got {type(q).__name__}")
    count = coerce_typed(count, "count", int)
    validate_max_concurrent(max_concurrent, example="meiva.web.search(queries, max_concurrent=4)")
    return SearchPlan(queries=queries, count=count, max_concurrent=max_concurrent)


def plan_fetch(
    targets: list[tuple[str, str]],
    max_chars: int,
    effort: str | ReasoningEffort | None,
    max_concurrent: int | None,
) -> FetchPlan:
    """Check the targets and coerce `max_chars` and `effort`; raise on the first invalid input."""
    if not isinstance(targets, list):
        raise TypeError(
            f"fetch() takes a list of (url, prompt) pairs, got {type(targets).__name__}. "
            f"Example: meiva.web.fetch([('https://example.com', 'summarize this page')])"
        )

    max_chars = coerce_typed(max_chars, "max_chars", int)
    checked_effort: Any = coerce_str(effort, "effort", allow_none=True)
    for i, pair in enumerate(targets):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(f"targets[{i}] must be a (url, prompt) pair, got {type(pair).__name__}")
        url, prompt = pair
        if not isinstance(url, str) or not isinstance(prompt, str):
            raise TypeError(
                f"targets[{i}] must be (url: str, prompt: str), "
                f"got ({type(url).__name__}, {type(prompt).__name__})"
            )

    resolved = coerce_effort(checked_effort, example="meiva.web.fetch(targets, effort='high')")
    validate_max_concurrent(max_concurrent, example="meiva.web.fetch(targets, max_concurrent=4)")
    return FetchPlan(
        targets=targets, max_chars=max_chars, effort=resolved, max_concurrent=max_concurrent
    )
