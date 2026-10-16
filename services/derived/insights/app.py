# ava:ref services/derived/insights/docs/insights.ava.okf.md#insights-service-ownership
from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline import router as run_timeline_router
from services.derived.insights.run_timeline.projection.jobs import IndexJobs, index_next
from services.derived.insights.run_timeline.projection.reader import TimelineReader


def build_app(
    db: Database,
    pool: ConnectionPool[Any],
    config: InsightsConfig,
    *,
    home: Path,
    catalog: ModelCatalog,
    timeline_inputs: TimelineReadInputs,
    default_model_reader: Callable[[], str],
) -> FastAPI:
    jobs = IndexJobs(
        partial(index_next, db, inputs=timeline_inputs, read_segment=read_checkpoint_segment)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(
        title="ava-insights", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.db = db
    app.state.db_pool = pool
    app.state.config = config
    app.state.catalog = catalog
    app.state.default_model_reader = default_model_reader
    app.state.timeline_inputs = timeline_inputs
    app.state.run_timeline_reader = TimelineReader(
        db,
        pool,
        inputs=timeline_inputs,
        jobs=jobs,
        read_segment=read_checkpoint_segment,
    )
    app.include_router(run_timeline_router.router)

    @app.get(
        "/healthz",
        description="Identity for the supervisor's probe: which service, which home, which process.",
    )
    async def healthz() -> dict[str, object]:
        return {"name": "insights", "pid": os.getpid(), "home": str(home)}

    return app
