"""Repo change classification — which side (frontend / backend) a set of changed
file paths touches.

CI's change-class gating (the `classify` job in `.github/workflows/ci.yml`) and the
PR test selector (`scripts/ci/test_selector.py`) both classify a diff through it.
"""

from __future__ import annotations

_DOC_ROOTS = (
    "decisions/",  # the why axis — never-rewritten ADRs
    "postmortems/",  # the why-it-escaped axis — frozen incident narratives
    "future/",  # the plans axis
    "conventions/",  # the how-to-work axis
    "okf/",  # the OKF index layer
    "assets/",  # README-embedded artifacts
    "schedules/",  # version-controlled schedule templates (provisioned, not imported)
)


def is_doc_path(path: str) -> bool:
    """Docs need no service restart: under a doc axis/artifact root, or a
    top-level Markdown file (README.md / AGENTS.md). A nested *.md
    (e.g. ui/web/AGENTS.md) is classified by its directory, not here.
    """
    return path.startswith(_DOC_ROOTS) or (path.endswith(".md") and "/" not in path)


def classify_change(paths: list[str]) -> tuple[bool, bool]:
    """Map changed file paths to (frontend_changed, backend_changed).

    frontend = under `ui/web/`; backend = anything else that isn't a pure doc.
    A docs-only (or empty) diff yields (False, False) -> nothing to restart.
    """
    frontend = backend = False
    for p in paths:
        if p.startswith("ui/web/"):
            frontend = True
        elif is_doc_path(p):
            continue
        else:
            backend = True
    return frontend, backend
