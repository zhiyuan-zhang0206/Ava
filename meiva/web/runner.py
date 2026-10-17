"""Run a planned search or fetch batch against the deps."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from meiva.sdk_surface.batch import run_batch
from meiva.web import brave, reader
from meiva.web.deps import FetchDeps, SearchDeps
from meiva.web.plans import FetchPlan, SearchPlan

__all__ = ["run_fetch", "run_search"]


def run_search[Result](
    plan: SearchPlan, deps: SearchDeps, make_result: Callable[..., Result]
) -> list[list[Result]]:
    """One result list per query, in input order; `make_result` builds each result."""
    return asyncio.run(
        run_batch(
            plan.queries,
            lambda q: brave.search_one(q, plan.count, deps, make_result),
            plan.max_concurrent,
        )
    )


def run_fetch(plan: FetchPlan, deps: FetchDeps) -> list[str]:
    """One answer per `(url, prompt)` pair, in input order."""
    # The answer step's per-invoke bound lives in the provider client
    # (build_chat_model timeout) — see meiva/understand/answer.py for why a
    # batch-layer wait_for would not actually return early. The page-read
    # step has its own MEIVA_WEB_FETCH_TIMEOUT_SECONDS bound.
    return asyncio.run(
        run_batch(
            plan.targets,
            lambda pair: reader.fetch_one(pair[0], pair[1], plan.max_chars, plan.effort, deps),
            plan.max_concurrent,
        )
    )
