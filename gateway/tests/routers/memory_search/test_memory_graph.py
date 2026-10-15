from __future__ import annotations

from pathlib import Path

from gateway.tests.routers.test_memory_search import (
    memory_config,
)
from gateway.tests.routers.test_memory_search import (
    memory_search_app_db as memory_search_app_db,
)
from tests.fixtures.gateway_config import gateway_test_client


class TestMemoryGraph:
    def test_graph_endpoint_reads_memory_root(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha", "beta"]
        by_id = {node["id"]: node for node in body["nodes"]}
        assert by_id["/"]["kind"] == "folder"
        assert by_id["alpha"]["kind"] == "note"
        assert by_id["alpha"]["primary_tag"] == "ava-internal"
        assert body["edges"] == [
            {"source": "alpha", "target": "/", "kind": "containment"},
            {"source": "beta", "target": "/", "kind": "containment"},
            {"source": "alpha", "target": "beta", "kind": "reference"},
        ]

    def test_graph_folder_pseudo_node_shape(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
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

    def test_graph_scans_subdirectories(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        ids = [node["id"] for node in body["nodes"]]
        assert "root-note" in ids
        assert "subdir/sub-note" in ids
        assert "subdir/" in ids
        edges = {(e["source"], e["target"], e["kind"]) for e in body["edges"]}
        assert ("root-note", "subdir/sub-note", "reference") in edges
        assert ("subdir/sub-note", "root-note", "reference") in edges
        assert ("root-note", "/", "containment") in edges
        assert ("subdir/sub-note", "subdir/", "containment") in edges
        assert ("subdir/", "/", "containment") in edges

    def test_graph_nested_folder_tree_gets_one_pseudo_node_each(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
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

    def test_graph_root_folder_present_even_without_root_level_notes(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "sub/", "sub/only"]
        assert {e["source"] for e in body["edges"]} == {"sub/only", "sub/"}
        assert ("sub/", "/", "containment") in {
            (e["source"], e["target"], e["kind"]) for e in body["edges"]
        }

    def test_graph_empty_pool_has_no_nodes_or_edges(self, tmp_path: Path) -> None:
        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["warnings"] == []

    def test_graph_out_of_root_link_is_skipped_not_500(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha"]
        assert body["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_non_list_tags_do_not_500(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "alpha", "beta"]
        assert all(node["tags"] == [] for node in body["nodes"])

    def test_graph_skips_reserved_names(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert [node["id"] for node in resp.json()["nodes"]] == ["/", "real"]

    def test_graph_skips_files_without_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "plain.md").write_text("# Just a heading\n\nNo YAML.\n", encoding="utf-8")
        (tmp_path / "note.md").write_text(
            """---
type: Memory
title: Note
---

# Note
"""
        )

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert [node["id"] for node in resp.json()["nodes"]] == ["/", "note"]

    def test_graph_missing_root_returns_warning(self, tmp_path: Path) -> None:
        with gateway_test_client(config=memory_config(tmp_path / "nope")) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == [] and body["edges"] == []
        assert body["warnings"] == ["memory_root not found"]

    def test_graph_unreadable_file_warns_and_skips(self, tmp_path: Path) -> None:
        (tmp_path / "x.md").mkdir()
        (tmp_path / "good.md").write_text(
            """---
type: Memory
title: Good
---

# Good
"""
        )

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        body = resp.json()
        assert [node["id"] for node in body["nodes"]] == ["/", "good"]
        assert any("cannot read" in w for w in body["warnings"])

    def test_graph_url_and_anchor_links_do_not_create_edges(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert resp.json()["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_dangling_edges_are_filtered(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        assert resp.json()["edges"] == [{"source": "alpha", "target": "/", "kind": "containment"}]

    def test_graph_stringifies_timestamp_and_agent(self, tmp_path: Path) -> None:
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

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        by_id = {node["id"]: node for node in resp.json()["nodes"]}
        assert by_id["alpha"]["timestamp"] is not None
        assert by_id["alpha"]["ava_agent"] == "1609"
        assert by_id["beta"]["timestamp"] is None
        assert by_id["beta"]["ava_agent"] is None


def test_primary_tag_prefers_a_domain_tag_over_the_type_tag() -> None:
    from gateway.routers.memory import primary_tag

    assert primary_tag(["type/env", "tech-ops"]) == "tech-ops"
    assert primary_tag(["tech-ops", "type/env"]) == "tech-ops"


def test_primary_tag_falls_back_to_the_type_tag_when_it_is_all_there_is() -> None:
    from gateway.routers.memory import primary_tag

    assert primary_tag(["type/user"]) == "type/user"
    assert primary_tag([]) == ""
