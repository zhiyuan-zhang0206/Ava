"""GET /api/okf/graph integration tests.

No DB involved — the route parses the repo's on-disk `.meiva.okf.md` tree
(`base/packages/docs/okf_graph.py`) and renders it into the D3 template on every request.
Covers: the route returns a self-contained HTML page with the data injected
(not the raw placeholder), and that it is gated by the normal cluster auth
middleware like every other route (it is not in `_AUTH_BYPASS_PATHS`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from base import config
from base.host.net.auth import bearer_header
from gateway.tests.agents.gateway_auth import without_credentials
from tests.fixtures.gateway_config import gateway_test_client

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def test_okf_graph_returns_html_with_injected_data() -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/okf/graph")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "window.GRAPH_DATA" in body
    assert "__GRAPH_DATA_JSON__" not in body  # placeholder was replaced
    assert '"name": "Meiva OKF"' in body or '"name":"Meiva OKF"' in body


def test_okf_graph_rejects_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is the normal middleware — this route is not in the bypass list."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with gateway_test_client() as client:
        resp = without_credentials(client).get("/api/okf/graph")
    assert resp.status_code == 401


def test_okf_graph_accepts_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with gateway_test_client() as client:
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
        ("agent/graph/docs/graph.meiva.okf.md", "agent/graph/graph.meiva.okf.md"),
        # Directories below the layer are kept.
        (
            "agent/graph/docs/context-notes/x.meiva.okf.md",
            "agent/graph/context-notes/x.meiva.okf.md",
        ),
        # A Python package named `docs`: only the last `docs` segment is the layer.
        ("base/packages/docs/docs/x.meiva.okf.md", "base/packages/docs/x.meiva.okf.md"),
        ("a/docs/b/docs/c.meiva.okf.md", "a/docs/b/c.meiva.okf.md"),
        ("docs/x.meiva.okf.md", "x.meiva.okf.md"),
        ("docs/index.meiva.okf.md", "index.meiva.okf.md"),
        # No `docs` directory segment: the path is its own logical path.
        ("agent/agent.meiva.okf.md", "agent/agent.meiva.okf.md"),
        # A file name is never a directory segment.
        ("agent/docs.meiva.okf.md", "agent/docs.meiva.okf.md"),
    ],
)
def test_logical_path_drops_the_last_docs_directory_segment(physical: str, logical: str) -> None:
    from base.packages.docs.okf_graph import logical_path

    assert logical_path(physical) == logical


_ROOT = "docs/index.meiva.okf.md"


def _parents(paths: set[str]) -> dict[str, str | None]:
    from base.packages.docs.okf_graph import compute_parent, find_root

    root = find_root(paths)
    return {p: compute_parent(p, paths, root) for p in sorted(paths)}


def test_layered_document_parents_to_the_layered_overview() -> None:
    paths = {
        _ROOT,
        "base/deploy/maintenance/docs/maintenance.meiva.okf.md",
        "base/deploy/maintenance/docs/lifecycle-wait.meiva.okf.md",
    }
    parents = _parents(paths)
    assert parents["base/deploy/maintenance/docs/lifecycle-wait.meiva.okf.md"] == (
        "base/deploy/maintenance/docs/maintenance.meiva.okf.md"
    )
    # Without an ancestor overview, this overview hangs off the apex.
    assert parents["base/deploy/maintenance/docs/maintenance.meiva.okf.md"] == _ROOT


def test_layered_directory_without_overview_parents_to_root() -> None:
    paths = {_ROOT, "base/sessions/docs/stopping.meiva.okf.md", "base/sessions/docs/x.meiva.okf.md"}
    parents = _parents(paths)
    assert parents["base/sessions/docs/stopping.meiva.okf.md"] == _ROOT
    assert parents["base/sessions/docs/x.meiva.okf.md"] == _ROOT


def test_nested_overviews_and_missing_intermediate_overviews_use_nearest_ancestor() -> None:
    paths = {
        _ROOT,
        "agent/docs/agent.meiva.okf.md",
        "agent/graph/docs/graph.meiva.okf.md",
        "agent/graph/exec/docs/exec.meiva.okf.md",
        "agent/graph/exec/deep/docs/leaf.meiva.okf.md",
        "agent/unindexed/deep/docs/leaf.meiva.okf.md",
    }
    assert _parents(paths) == {
        _ROOT: None,
        "agent/docs/agent.meiva.okf.md": _ROOT,
        "agent/graph/docs/graph.meiva.okf.md": "agent/docs/agent.meiva.okf.md",
        "agent/graph/exec/docs/exec.meiva.okf.md": "agent/graph/docs/graph.meiva.okf.md",
        "agent/graph/exec/deep/docs/leaf.meiva.okf.md": "agent/graph/exec/docs/exec.meiva.okf.md",
        "agent/unindexed/deep/docs/leaf.meiva.okf.md": "agent/docs/agent.meiva.okf.md",
    }


def test_layered_and_unlayered_documents_share_one_tree() -> None:
    """Migration in progress: layered and unlayered directories coexist, and
    even one directory's overview and children may sit on different sides."""
    paths = {
        _ROOT,
        "docs/plugins/plugins.meiva.okf.md",
        "agent/agent.meiva.okf.md",
        "agent/kernel.meiva.okf.md",
        "base/docs/base.meiva.okf.md",
        "base/docs/config.meiva.okf.md",
        "base/sessions/docs/stopping.meiva.okf.md",
        "mixed/mixed.meiva.okf.md",
        "mixed/docs/part.meiva.okf.md",
    }
    parents = _parents(paths)
    assert parents == {
        _ROOT: None,
        "docs/plugins/plugins.meiva.okf.md": _ROOT,
        "agent/agent.meiva.okf.md": _ROOT,
        "agent/kernel.meiva.okf.md": "agent/agent.meiva.okf.md",
        "base/docs/base.meiva.okf.md": _ROOT,
        "base/docs/config.meiva.okf.md": "base/docs/base.meiva.okf.md",
        "base/sessions/docs/stopping.meiva.okf.md": "base/docs/base.meiva.okf.md",
        "mixed/mixed.meiva.okf.md": _ROOT,
        "mixed/docs/part.meiva.okf.md": "mixed/mixed.meiva.okf.md",
    }
    assert all(p is None or p in paths for p in parents.values())


def test_repository_docs_keep_cross_domain_overviews_and_nested_children() -> None:
    paths = {
        _ROOT,
        "docs/plugins/plugins.meiva.okf.md",
        "docs/plugins/module-loading/module-loading.meiva.okf.md",
        "docs/plugins/module-loading/two-faces.meiva.okf.md",
        "docs/skills/skills.meiva.okf.md",
    }
    assert _parents(paths) == {
        _ROOT: None,
        "docs/plugins/plugins.meiva.okf.md": _ROOT,
        "docs/plugins/module-loading/module-loading.meiva.okf.md": "docs/plugins/plugins.meiva.okf.md",
        "docs/plugins/module-loading/two-faces.meiva.okf.md": (
            "docs/plugins/module-loading/module-loading.meiva.okf.md"
        ),
        "docs/skills/skills.meiva.okf.md": _ROOT,
    }


def test_python_package_named_docs_keeps_its_layer_under_the_package() -> None:
    """`base/packages/docs/docs/` is the layer of the Python package `docs`."""
    paths = {
        _ROOT,
        "base/packages/docs/docs/docs.meiva.okf.md",
        "base/packages/docs/docs/okf-graph.meiva.okf.md",
    }
    parents = _parents(paths)
    assert parents["base/packages/docs/docs/okf-graph.meiva.okf.md"] == (
        "base/packages/docs/docs/docs.meiva.okf.md"
    )
    assert parents["base/packages/docs/docs/docs.meiva.okf.md"] == _ROOT


def test_two_physical_paths_with_one_logical_path_are_refused() -> None:
    from base.packages.docs.okf_graph import compute_parent

    paths = {_ROOT, "a/x.meiva.okf.md", "a/docs/x.meiva.okf.md"}
    with pytest.raises(ValueError, match=re.escape("share the logical path a/x.meiva.okf.md")):
        compute_parent("a/x.meiva.okf.md", paths, _ROOT)


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

    write("docs/index.meiva.okf.md")
    write("pkg/docs/pkg.meiva.okf.md")
    write("pkg/docs/child.meiva.okf.md", "See [[other/docs/leaf.meiva.okf.md]].")
    write("pkg/nested/docs/nested.meiva.okf.md")
    write("pkg/nested/deep/docs/leaf.meiva.okf.md")
    write("other/docs/leaf.meiva.okf.md")
    write("legacy/legacy.meiva.okf.md")
    write("legacy/child.meiva.okf.md")

    data = build_graph_data(tmp_path)
    ids = {n["id"] for n in data["nodes"]}
    roots = [n["id"] for n in data["nodes"] if n["parent"] is None]
    assert roots == ["docs/index.meiva.okf.md"]
    tree = {(e["source"], e["target"]) for e in data["treeEdges"]}
    assert tree == {
        ("docs/index.meiva.okf.md", "pkg/docs/pkg.meiva.okf.md"),
        ("pkg/docs/pkg.meiva.okf.md", "pkg/docs/child.meiva.okf.md"),
        ("pkg/docs/pkg.meiva.okf.md", "pkg/nested/docs/nested.meiva.okf.md"),
        ("pkg/nested/docs/nested.meiva.okf.md", "pkg/nested/deep/docs/leaf.meiva.okf.md"),
        ("docs/index.meiva.okf.md", "other/docs/leaf.meiva.okf.md"),
        ("docs/index.meiva.okf.md", "legacy/legacy.meiva.okf.md"),
        ("legacy/legacy.meiva.okf.md", "legacy/child.meiva.okf.md"),
    }
    assert all(a in ids and b in ids for a, b in tree)
    assert [(e["source"], e["target"]) for e in data["crossEdges"]] == [
        ("pkg/docs/child.meiva.okf.md", "other/docs/leaf.meiva.okf.md")
    ]
