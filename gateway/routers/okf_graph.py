"""OKF knowledge-graph viewer — GET /api/okf/graph.

Serves the D3 force-directed visualization of the repo's `*.ava.okf.md`
documentation bundle (see `index.ava.okf.md`) as a single self-contained HTML
page. Rebuilt fresh from the current `.ava.okf.md` tree on every request —
this route never reads the checked-in `graph_data.json` (a separate,
manually-refreshed convention used for reviewing doc changes as a diff), so
the served graph cannot go stale between manual rebuilds.

This is the one deliberate exception to gateway/app.py's "pure JSON API,
does not serve HTML" rule (see that module's docstring): it reuses the
existing dev-tool template (`okf-d3-template.html`) and the build/inject
mechanics in `shared/okf_graph.py` — also used by `scripts/serve_okf_viz.py`'s
local CLI — rather than reimplementing the D3 rendering as a frontend
component. Auth is the normal gateway session-cookie / bearer-secret
middleware (`gateway/app.py`'s `_cluster_auth_middleware`) — this path is not
in `_AUTH_BYPASS_PATHS`, so it requires the same login as every other route.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from shared.okf_graph import build_graph_data, render_html
from shared.paths import repo_root

router = APIRouter()

_TEMPLATE_NAME = "okf-d3-template.html"


@router.get("/api/okf/graph")
def get_okf_graph() -> HTMLResponse:
    """Build the OKF graph from the live `.ava.okf.md` tree and render it
    into the self-contained D3 template — open directly in a browser tab."""
    repo = repo_root()
    template_text = (repo / "scripts" / _TEMPLATE_NAME).read_text(encoding="utf-8")
    data = build_graph_data(repo, name="Ava OKF")
    return HTMLResponse(render_html(data, template_text))
