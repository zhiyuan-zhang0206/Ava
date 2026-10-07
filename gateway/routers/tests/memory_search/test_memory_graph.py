"""Memory search cases: memory graph."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers.tests.test_memory_search import _app_db as _app_db
from gateway.routers.tests.test_memory_search import _legacy_extract_meta


class TestMemoryGraph:
    def test_graph_endpoint_reads_memory_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:

        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
---

See [Beta](beta.md).
""",
            encoding="utf-8",
        )
        (tmp_path / "beta.md").write_text(
            """---
type: Memory
title: Beta
tags: [tech-ops]
---

# Beta
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        # Folder pseudo nodes come first (root "/", then subfolders), notes after.
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha", "beta"]
        by_id = {node["id"]: node for node in body["nodes"]}
        assert by_id["/"]["kind"] == "folder"
        assert by_id["alpha"]["kind"] == "note"
        assert by_id["alpha"]["primary_tag"] == "ava-internal"
        # Containment edges (note → folder) form the main structure; the
        # markdown cross-link is a weak reference edge.
        assert body["edges"] == [
            {"source": "alpha", "target": "/", "kind": "containment"},
            {"source": "beta", "target": "/", "kind": "containment"},
            {"source": "alpha", "target": "beta", "kind": "reference"},
        ]

    def test_graph_folder_pseudo_node_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The folder pseudo node carries only structure — display fields stay empty."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
---

# Alpha
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        by_id = {node["id"]: node for node in resp.json()["nodes"]}
        assert by_id["/"] == {
            "id": "/",
            "path": "/",
            "title": tmp_path.name,
            "kind": "folder",
            "description": None,
            "tags": [],
            "primary_tag": "",
            "timestamp": None,
            "ava_agent": None,
            "ava_machine": None,
        }

    def test_graph_scans_subdirectories(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Recursive rglob picks up notes in subdirectories."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)

        # Root-level file
        (tmp_path / "root-note.md").write_text(
            """---
type: Memory
title: Root Note
tags: [ava-internal]
---

See [Sub Note](subdir/sub-note.md).
""",
            encoding="utf-8",
        )

        # Subdirectory
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "sub-note.md").write_text(
            """---
type: Memory
title: Sub Note
tags: [tech-ops]
---

See [Root Note](../root-note.md).
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        ids = [node["id"] for node in body["nodes"]]
        # Both notes are included, subdirectory note has "subdir/" prefix,
        # and each folder (root + subdir) gets a pseudo node.
        assert "root-note" in ids
        assert "subdir/sub-note" in ids
        assert "subdir/" in ids
        # Bidirectional reference edges, plus containment: each note → its
        # folder and the subfolder → root.
        edges = {(e["source"], e["target"], e["kind"]) for e in body["edges"]}
        assert ("root-note", "subdir/sub-note", "reference") in edges
        assert ("subdir/sub-note", "root-note", "reference") in edges
        assert ("root-note", "/", "containment") in edges
        assert ("subdir/sub-note", "subdir/", "containment") in edges
        assert ("subdir/", "/", "containment") in edges

    def test_graph_nested_folder_tree_gets_one_pseudo_node_each(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One pseudo node per folder, closed under parent directories, with
        folder → parent-folder containment edges forming a rooted tree."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "root.md").write_text(
            """---
type: Memory
title: Root
tags: [ava-internal]
---

# Root
""",
            encoding="utf-8",
        )
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "mid.md").write_text(
            """---
type: Memory
title: Mid
tags: [ava-internal]
---

# Mid
""",
            encoding="utf-8",
        )
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "deep.md").write_text(
            """---
type: Memory
title: Deep
tags: [ava-internal]
---

# Deep
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == [
            "/",
            "a/",
            "a/b/",
            "a/b/deep",
            "a/mid",
            "root",
        ]
        by_id = {node["id"]: node for node in body["nodes"]}
        assert by_id["a/"]["title"] == "a"
        assert by_id["a/b/"]["title"] == "b"
        assert body["edges"] == [
            {"source": "a/b/deep", "target": "a/b/", "kind": "containment"},
            {"source": "a/mid", "target": "a/", "kind": "containment"},
            {"source": "root", "target": "/", "kind": "containment"},
            {"source": "a/", "target": "/", "kind": "containment"},
            {"source": "a/b/", "target": "a/", "kind": "containment"},
        ]

    def test_graph_root_folder_present_even_without_root_level_notes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The pool root is always a pseudo node so the skeleton stays one
        connected tree even when every note lives in a subdirectory."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "only.md").write_text(
            """---
type: Memory
title: Only
tags: [ava-internal]
---

# Only
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "sub/", "sub/only"]
        assert {e["source"] for e in body["edges"]} == {"sub/only", "sub/"}
        assert ("sub/", "/", "containment") in {
            (e["source"], e["target"], e["kind"]) for e in body["edges"]
        }

    def test_graph_empty_pool_has_no_nodes_or_edges(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An existing but empty pool yields an empty graph — no lone root folder."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["warnings"] == []

    # ── behavior locks (audit #2448: out-of-root links / bad tags used to 500) ──

    def test_graph_out_of_root_link_is_skipped_not_500(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A link escaping memory_root (../ or absolute) must not 500 the whole
        endpoint — the edge is dropped instead (one bad note must not kill the
        graph page)."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
---

See [Outside](../outside.md) and [Absolute](/etc/passwd).
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha"]
        assert body["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_non_list_tags_do_not_500(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`tags: 5` used to TypeError (500); `tags: {a: 1}` used to smuggle
        dict keys in as tags. Both now yield an empty tag list — tags are a
        string list, nothing else."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: 5
---

# Alpha
""",
            encoding="utf-8",
        )
        (tmp_path / "beta.md").write_text(
            """---
type: Memory
title: Beta
tags: {a: 1}
---

# Beta
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha", "beta"]
        assert all(node["tags"] == [] for node in body["nodes"])

    def test_graph_skips_reserved_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OKF-reserved files (index.md / log.md / MEMORY.md) never become nodes."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        for name in ("index.md", "log.md", "MEMORY.md"):
            (tmp_path / name).write_text(
                """---
type: Memory
title: Reserved
---

# Reserved
""",
                encoding="utf-8",
            )
        (tmp_path / "real.md").write_text(
            """---
type: Memory
title: Real
---

# Real
"""
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert [node["id"] for node in resp.json()["nodes"]] == ["/", "real"]

    def test_graph_skips_files_without_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A file without `---` frontmatter is not a note and never becomes a node."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "plain.md").write_text("# Just a heading\n\nNo YAML.\n", encoding="utf-8")
        (tmp_path / "note.md").write_text(
            """---
type: Memory
title: Note
---

# Note
"""
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert [node["id"] for node in resp.json()["nodes"]] == ["/", "note"]

    def test_graph_missing_root_returns_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """memory_root missing → 200 + a warning, not a 500."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path / "nope")

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == [] and body["edges"] == []
        assert body["warnings"] == ["memory_root not found"]

    def test_graph_unreadable_file_warns_and_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreadable file (here: a directory named `x.md/`) produces a
        warning and is skipped instead of 500-ing."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "x.md").mkdir()
        (tmp_path / "good.md").write_text(
            """---
type: Memory
title: Good
---

# Good
"""
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "good"]
        assert any("cannot read" in w for w in body["warnings"])

    def test_graph_url_and_anchor_links_do_not_create_edges(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """http(s):// and # links are not concept edges."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
---

See [Web](https://example.com) and [Anchor](#section).
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert resp.json()["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_dangling_edges_are_filtered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A reference edge pointing at a note that does not exist in the pool
        is dropped; containment edges are unaffected."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
---

See [Ghost](ghost.md).
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert resp.json()["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_stringifies_timestamp_and_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """timestamp / ava_agent: truthy → str(), falsy/missing → None."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
tags: [ava-internal]
timestamp: 2026-01-02 03:04:05
ava_agent: 1609
---

# Alpha
""",
            encoding="utf-8",
        )
        (tmp_path / "beta.md").write_text(
            """---
type: Memory
title: Beta
---

# Beta
"""
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        by_id = {node["id"]: node for node in resp.json()["nodes"]}
        # YAML parses the unquoted timestamp as a datetime → str() renders it back
        assert by_id["alpha"]["timestamp"] is not None
        assert by_id["alpha"]["ava_agent"] == "1609"
        assert by_id["beta"]["timestamp"] is None
        assert by_id["beta"]["ava_agent"] is None


def test_primary_tag_prefers_a_domain_tag_over_the_type_tag() -> None:
    """Every note carries a `type/<x>`, so grouping the graph on it would
    collapse the view into six buckets and hide the domain structure it exists
    to show."""
    from gateway.routers.memory import _primary_tag

    assert _primary_tag(["type/env", "tech-ops"]) == "tech-ops"
    assert _primary_tag(["tech-ops", "type/env"]) == "tech-ops"


def test_primary_tag_falls_back_to_the_type_tag_when_it_is_all_there_is() -> None:
    """Better a coarse label than an unlabeled node."""
    from gateway.routers.memory import _primary_tag

    assert _primary_tag(["type/user"]) == "type/user"
    assert _primary_tag([]) == ""


def test_extract_meta_equivalent_to_legacy(tmp_path: Path) -> None:
    """Description/tags extraction matches the old inline parser on the three
    input classes it promised to handle: normal, no frontmatter, bad YAML."""
    from gateway.routers.memory import _extract_meta

    cases = {
        "normal.md": (
            "---\ntype: Memory\ndescription: A note about health\ntags: [type/user, health]\n"
            "---\n\n# Health\n"
        ),
        "no_fm.md": "# Just a heading\n",
        "bad_yaml.md": "---\ntags: [unclosed\n---\nb\n",
        "unterminated.md": "---\ntitle: X\n",
        "non_dict.md": "---\n- a\n- list\n---\nb\n",
        "blank_desc.md": "---\ndescription: \ntags: [a]\n---\nb\n",
    }
    for name, body in cases.items():
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        assert _extract_meta(p) == _legacy_extract_meta(p), name


def test_extract_meta_unreadable_returns_empty(tmp_path: Path) -> None:
    from gateway.routers.memory import _extract_meta

    assert _extract_meta(tmp_path / "missing.md") == ("", [])
    assert _extract_meta(tmp_path) == ("", [])  # a directory — read_text raises IsADirectoryError


def test_extract_meta_coerces_non_string_description(tmp_path: Path) -> None:
    """`description: 123` used to 500 the graph endpoint (pydantic str field);
    it now surfaces as its string form instead."""
    from gateway.routers.memory import _extract_meta

    p = tmp_path / "scalar.md"
    p.write_text("---\ndescription: 123\ntags: 5\n---\nb\n", encoding="utf-8")
    assert _extract_meta(p) == ("123", [])
