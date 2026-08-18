"""The single concurrent batch executor behind the SDK batch APIs.

`ava.web.search` / `ava.web.fetch` / `ava.understand` share one batch pattern —
validate items, validate knobs, then schedule every worker concurrently with
order preserved and the first error propagated. Before this module the
semaphore scheduling lived in three copies (`_search_all` / `_fetch_all` /
`_understand_all`); `run_batch` is the one place it exists now, and
`validate_max_concurrent` the one place the `max_concurrent` knob is checked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


def validate_max_concurrent(max_concurrent: int | None, *, example: str) -> None:
    """Validate the `max_concurrent` knob shared by every SDK batch API.

    `example` names the calling API in error messages (each entry point keeps
    its own wording). Fail-fast before any work is scheduled.

    Raises:
        TypeError: not an int or None (bool is rejected — it is an int
            subclass, but `True` as a concurrency cap is a bug).
        ValueError: less than 1 — 0 or negative would block every call forever.
    """
    if max_concurrent is not None:
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool):
            raise TypeError(
                f"max_concurrent must be an int or None, got {type(max_concurrent).__name__}. "
                f"Example: {example}"
            )
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be at least 1, got {max_concurrent} — "
                "0 or negative would block every call forever"
            )


async def run_batch[T, R](
    items: list[T],
    worker: Callable[[T], R],
    max_concurrent: int | None,
    after: Callable[[T, R], None] | None = None,
) -> list[R]:
    """Run `worker` over every item concurrently.

    Order is preserved; the first error propagates. `max_concurrent` caps how
    many workers are in flight at once via a semaphore; None keeps concurrency
    unbounded — a provider 429 propagates like any other upstream failure.

    `after` (optional) is called once per item, in input order, on the event
    loop thread after the batch collected — for per-result bookkeeping (e.g.
    `ava.understand`'s auto-save) that must not race inside worker threads.
    An `after` failure propagates like a worker failure.
    """
    validate_max_concurrent(max_concurrent, example="<batch api>(..., max_concurrent=N)")
    if max_concurrent is None:
        tasks = [asyncio.to_thread(worker, item) for item in items]
    else:
        sem = asyncio.Semaphore(max_concurrent)

        async def _bounded(item: T) -> R:
            async with sem:
                return await asyncio.to_thread(worker, item)

        tasks = [_bounded(item) for item in items]
    results = await asyncio.gather(*tasks)
    if after is not None:
        for item, result in zip(items, results, strict=True):
            after(item, result)
    return results
