"""GET /api/okf/graph integration tests.

No DB involved — the route parses the repo's on-disk `.ava.okf.md` tree
(`shared/okf_graph.py`) and renders it into the D3 template on every request.
Covers: the route returns a self-contained HTML page with the data injected
(not the raw placeholder), and that it is gated by the normal cluster auth
middleware like every other route (it is not in `_AUTH_BYPASS_PATHS`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config
from shared.cluster_auth import bearer_header

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def test_okf_graph_returns_html_with_injected_data() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/okf/graph")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "window.GRAPH_DATA" in body
    assert "__GRAPH_DATA_JSON__" not in body  # placeholder was replaced
    assert '"name": "Ava OKF"' in body or '"name":"Ava OKF"' in body


def test_okf_graph_rejects_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is the normal middleware — this route is not in the bypass list."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/okf/graph")
    assert resp.status_code == 401


def test_okf_graph_accepts_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/okf/graph", headers=bearer_header(_SECRET))
    assert resp.status_code == 200


# ── parse_frontmatter equivalence (audit #2448 Phase 2) ──
#
# The okf adapter now delegates to shared.frontmatter.parse_frontmatter_typed.
# These tests lock the merge: on every bundle in the repo the adapter must be
# field-for-field identical to the pre-refactor parser, so the shared-parser
# consolidation cannot silently change the OKF graph.


def _legacy_parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The pre-#2448 `okf_graph.parse_frontmatter` — reference implementation."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                loaded = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                loaded = None
            fm: dict[str, Any] = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, text


def test_parse_frontmatter_equivalent_to_legacy_on_repo_bundles() -> None:
    """Every `.ava.okf.md` bundle in the repo parses identically through the
    adapter and the legacy parser — frontmatter and body, field for field."""
    from shared.okf_graph import find_files, parse_frontmatter

    repo_root = Path(__file__).resolve().parents[2]
    bundles = find_files(repo_root)
    assert bundles, "repo has no .ava.okf.md bundles — fixture assumption broken"

    for rel in bundles:
        text = (repo_root / rel).read_text(encoding="utf-8")
        legacy_fm, legacy_body = _legacy_parse_frontmatter(text)
        fm, body = parse_frontmatter(text)
        assert fm == legacy_fm, f"{rel}: frontmatter differs from legacy"
        assert body == legacy_body, f"{rel}: body differs from legacy"


def test_parse_frontmatter_keeps_legacy_tolerance_for_spaced_fences() -> None:
    """`"--- "` opener/closer lines predate the shared parser; the adapter
    still accepts them (the strict shared parser rejects them)."""
    from shared.okf_graph import parse_frontmatter

    fm, body = parse_frontmatter("--- \ntitle: X\n--- \n\nBody\n")
    assert fm == {"title": "X"}
    assert body == "Body\n"
    # and bad frontmatter still degrades to ({}, text), not an exception
    assert parse_frontmatter("no frontmatter\n") == ({}, "no frontmatter\n")
