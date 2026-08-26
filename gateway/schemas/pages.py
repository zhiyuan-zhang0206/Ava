"""agent-published pages.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
)

_PageName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    ),
]


class PageRegisterRequest(BaseModel):
    """POST /api/agents/{aid}/pages request body — SDK ava.ui.show / .serve call.

    `host`: the agent-runner's reachable address (the agent self-reports
        it) — where the gateway's reverse proxy dials the page server
        (loopback on a single box). Never appears in any URL.
    `port`: port that the agent process's HTTP server is listening on
        (bound on the runner's reachable host, never 0.0.0.0). 1-65535.
    `title`: optional human-readable title shown in the frontend
        tab/header; falls back to name when not given.
    `serve_dir`: directory the page server serves, recorded by
        ava.ui.serve()/serve_markdown() so agent boot can re-serve a dead
        page server after resurrect/restart; absent for ava.ui.show()
        pages (the agent manages those servers itself).
    """

    name: _PageName
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., gt=0, lt=65536)
    title: str | None = Field(default=None, max_length=200)
    serve_dir: str | None = Field(default=None, max_length=4096)
    ttl_seconds: int | None = Field(default=None, gt=0)
