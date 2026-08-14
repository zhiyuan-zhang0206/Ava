"""Repo change classification — which side (frontend / backend) a set of changed
file paths touches.

Single source of truth shared by `ava cluster update`'s selective restart
(`cli/commands/update.py`) and the gateway's read-only update preflight
(`gateway/cluster.py:update_check`). The gateway cannot import the higher `cli`
layer, so this lives in `shared` where both can reach it — keeping the two from
drifting (a divergence would make the UI advertise a different restart side than
the rollout actually performs).
"""

from __future__ import annotations

_DOC_ROOTS = (
    "decisions/",  # the why axis — never-rewritten ADRs
    "future/",  # the plans axis
    "conventions/",  # the how-to-work axis
    "traces/",  # the runtime-record axis
    "okf/",  # the OKF index layer
    "assets/",  # README-embedded artifacts
    "schedules/",  # version-controlled schedule templates (provisioned, not imported)
)


def is_doc_path(path: str) -> bool:
    """Docs need no service restart: under a doc axis/artifact root, or a
    top-level Markdown file (CLAUDE.md / README.md / AGENTS.md). A nested *.md
    (e.g. frontend/CLAUDE.md) is classified by its directory, not here.
    """
    return path.startswith(_DOC_ROOTS) or (path.endswith(".md") and "/" not in path)


def classify_change(paths: list[str]) -> tuple[bool, bool]:
    """Map changed file paths to (frontend_changed, backend_changed).

    frontend = under `frontend/`; backend = anything else that isn't a pure doc.
    A docs-only (or empty) diff yields (False, False) -> nothing to restart.
    """
    frontend = backend = False
    for p in paths:
        if p.startswith("frontend/"):
            frontend = True
        elif is_doc_path(p):
            continue
        else:
            backend = True
    return frontend, backend
