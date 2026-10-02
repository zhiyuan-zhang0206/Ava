"""GET /api/okf/graph integration tests.

No DB involved — the route parses the repo's on-disk `.ava.okf.md` tree
(`base/packages/docs/okf_graph.py`) and renders it into the D3 template on every request.
Covers: the route returns a self-contained HTML page with the data injected
(not the raw placeholder), and that it is gated by the normal cluster auth
middleware like every other route (it is not in `_AUTH_BYPASS_PATHS`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from base import config
from base.cluster.auth import bearer_header
from gateway.app import app

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
# The okf adapter now delegates to base.packages.docs.frontmatter.parse_frontmatter_typed.
# These tests lock the merge: on every bundle in the repo the adapter must be
# field-for-field identical to the pre-refactor parser, so the shared-parser
# consolidation cannot silently change the OKF graph.


@pytest.mark.parametrize(
    "text",
    [
        "--- \ntitle: X\n--- \n\nBody\n",
        "---\ntitle: [unclosed\n---\nBody\n",
        "---\ntitle: X\nBody\n",
        "no frontmatter\n",
    ],
)
def test_parse_frontmatter_returns_original_text_for_unknown_or_bad_fences(text: str) -> None:
    """Unknown fences and structurally invalid blocks are plain body text."""
    from base.packages.docs.okf_graph import parse_frontmatter

    assert parse_frontmatter(text) == ({}, text)


# ── the transparent `docs/` layer ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("physical", "logical"),
    [
        ("agent/graph/docs/graph.ava.okf.md", "agent/graph/graph.ava.okf.md"),
        # Directories below the layer are kept.
        (
            "agent/graph/docs/context-notes/x.ava.okf.md",
            "agent/graph/context-notes/x.ava.okf.md",
        ),
        # A Python package named `docs`: only the last `docs` segment is the layer.
        ("base/packages/docs/docs/x.ava.okf.md", "base/packages/docs/x.ava.okf.md"),
        ("a/docs/b/docs/c.ava.okf.md", "a/docs/b/c.ava.okf.md"),
        ("docs/x.ava.okf.md", "x.ava.okf.md"),
        # No `docs` directory segment: the path is its own logical path.
        ("okf/index.ava.okf.md", "okf/index.ava.okf.md"),
        ("agent/agent.ava.okf.md", "agent/agent.ava.okf.md"),
        # A file name is never a directory segment.
        ("agent/docs.ava.okf.md", "agent/docs.ava.okf.md"),
    ],
)
def test_logical_path_drops_the_last_docs_directory_segment(physical: str, logical: str) -> None:
    from base.packages.docs.okf_graph import logical_path

    assert logical_path(physical) == logical


_ROOT = "okf/index.ava.okf.md"


def _parents(paths: set[str]) -> dict[str, str | None]:
    from base.packages.docs.okf_graph import compute_parent, find_root

    root = find_root(paths)
    return {p: compute_parent(p, paths, root) for p in sorted(paths)}


def test_layered_document_parents_to_the_layered_overview() -> None:
    paths = {
        _ROOT,
        "base/deploy/maintenance/docs/maintenance.ava.okf.md",
        "base/deploy/maintenance/docs/lifecycle-wait.ava.okf.md",
    }
    parents = _parents(paths)
    assert parents["base/deploy/maintenance/docs/lifecycle-wait.ava.okf.md"] == (
        "base/deploy/maintenance/docs/maintenance.ava.okf.md"
    )
    # An overview node hangs off the apex, like every overview.
    assert parents["base/deploy/maintenance/docs/maintenance.ava.okf.md"] == _ROOT


def test_layered_directory_without_overview_parents_to_root() -> None:
    paths = {_ROOT, "base/sessions/docs/stopping.ava.okf.md", "base/sessions/docs/x.ava.okf.md"}
    parents = _parents(paths)
    assert parents["base/sessions/docs/stopping.ava.okf.md"] == _ROOT
    assert parents["base/sessions/docs/x.ava.okf.md"] == _ROOT


def test_layered_and_unlayered_documents_share_one_tree() -> None:
    """Migration in progress: layered and unlayered directories coexist, and
    even one directory's overview and children may sit on different sides."""
    paths = {
        _ROOT,
        "okf/plugins.ava.okf.md",
        "agent/agent.ava.okf.md",
        "agent/kernel.ava.okf.md",
        "base/docs/base.ava.okf.md",
        "base/docs/config.ava.okf.md",
        "base/sessions/docs/stopping.ava.okf.md",
        "mixed/mixed.ava.okf.md",
        "mixed/docs/part.ava.okf.md",
    }
    parents = _parents(paths)
    assert parents == {
        _ROOT: None,
        "okf/plugins.ava.okf.md": _ROOT,
        "agent/agent.ava.okf.md": _ROOT,
        "agent/kernel.ava.okf.md": "agent/agent.ava.okf.md",
        "base/docs/base.ava.okf.md": _ROOT,
        "base/docs/config.ava.okf.md": "base/docs/base.ava.okf.md",
        "base/sessions/docs/stopping.ava.okf.md": _ROOT,
        "mixed/mixed.ava.okf.md": _ROOT,
        "mixed/docs/part.ava.okf.md": "mixed/mixed.ava.okf.md",
    }
    assert all(p is None or p in paths for p in parents.values())


def test_okf_index_layer_is_unchanged() -> None:
    """`okf/` is not a `docs` layer: its nodes have no filesystem parent."""
    paths = {_ROOT, "okf/plugins.ava.okf.md", "okf/skills.ava.okf.md"}
    assert _parents(paths) == {_ROOT: None, **dict.fromkeys(paths - {_ROOT}, _ROOT)}


def test_python_package_named_docs_keeps_its_layer_under_the_package() -> None:
    """`base/packages/docs/docs/` is the layer of the Python package `docs`."""
    paths = {
        _ROOT,
        "base/packages/docs/docs/docs.ava.okf.md",
        "base/packages/docs/docs/okf-graph.ava.okf.md",
    }
    parents = _parents(paths)
    assert parents["base/packages/docs/docs/okf-graph.ava.okf.md"] == (
        "base/packages/docs/docs/docs.ava.okf.md"
    )
    assert parents["base/packages/docs/docs/docs.ava.okf.md"] == _ROOT


def test_two_physical_paths_with_one_logical_path_are_refused() -> None:
    from base.packages.docs.okf_graph import compute_parent

    paths = {_ROOT, "a/x.ava.okf.md", "a/docs/x.ava.okf.md"}
    with pytest.raises(ValueError, match=re.escape("share the logical path a/x.ava.okf.md")):
        compute_parent("a/x.ava.okf.md", paths, _ROOT)


def test_graph_of_layered_bundle_has_one_root_and_physical_edges(tmp_path: Path) -> None:
    """End to end: layered nodes get tree edges to layered parents, wikilinks
    keep resolving to physical paths, and no edge dangles."""
    from base.packages.docs.okf_graph import build_graph_data

    def write(rel: str, body: str = "") -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: doc\ntitle: {rel}\ndescription: d\n---\n\n{body}\n", encoding="utf-8"
        )

    write("okf/index.ava.okf.md")
    write("pkg/docs/pkg.ava.okf.md")
    write("pkg/docs/child.ava.okf.md", "See [[other/docs/leaf.ava.okf.md]].")
    write("other/docs/leaf.ava.okf.md")
    write("legacy/legacy.ava.okf.md")
    write("legacy/child.ava.okf.md")

    data = build_graph_data(tmp_path)
    ids = {n["id"] for n in data["nodes"]}
    roots = [n["id"] for n in data["nodes"] if n["parent"] is None]
    assert roots == ["okf/index.ava.okf.md"]
    tree = {(e["source"], e["target"]) for e in data["treeEdges"]}
    assert tree == {
        ("okf/index.ava.okf.md", "pkg/docs/pkg.ava.okf.md"),
        ("pkg/docs/pkg.ava.okf.md", "pkg/docs/child.ava.okf.md"),
        ("okf/index.ava.okf.md", "other/docs/leaf.ava.okf.md"),
        ("okf/index.ava.okf.md", "legacy/legacy.ava.okf.md"),
        ("legacy/legacy.ava.okf.md", "legacy/child.ava.okf.md"),
    }
    assert all(a in ids and b in ids for a, b in tree)
    assert [(e["source"], e["target"]) for e in data["crossEdges"]] == [
        ("pkg/docs/child.ava.okf.md", "other/docs/leaf.ava.okf.md")
    ]
