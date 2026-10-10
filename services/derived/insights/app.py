"""The insights HTTP app: the routers of every read model, over one database.

Routes keep the paths the browser calls (`/api/agents/{id}/run-timeline...`), so the
gateway forwards a request unchanged. The service performs no authentication: it is
reachable only through its Unix socket, and the gateway has already admitted the caller.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.paths import ava_home
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline import history as run_timeline_history
from services.derived.insights.run_timeline import router as run_timeline_router


def build_app(
    db: Database,
    pool: ConnectionPool[Any],
    config: InsightsConfig,
    *,
    catalog: ModelCatalog,
    timeline_inputs: TimelineReadInputs,
    default_model_reader: Callable[[], str],
) -> FastAPI:
    """The app over `db` (checkpoint and audit reads), `pool` (short row lookups) and `config`.

    The caller owns both and closes them after the app has stopped serving.
    `timeline_inputs` supplies the same live rendering policy to history derivation and messages.
    """
    app = FastAPI(title="ava-insights", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.db_pool = pool
    app.state.config = config
    app.state.catalog = catalog
    app.state.default_model_reader = default_model_reader
    app.state.timeline_inputs = timeline_inputs
    app.state.run_timeline_views = run_timeline_history.HistoryViewCache(
        timeline_inputs=timeline_inputs
    )
    app.include_router(run_timeline_router.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        """Identity for the supervisor's probe: which service, which home, which process."""
        return {"name": "insights", "pid": os.getpid(), "home": str(ava_home())}

    return app
