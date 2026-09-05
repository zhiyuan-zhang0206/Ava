"""`POST /api/memory/search` endpoint unit tests — primary direct embed+milvus;
relative-path conversion; wire error propagation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from services.memory_indexer.embeddings.base import EmbeddingAPIError


class _StubProvider:
    """Embedding provider stand-in — the handler reads dim/fingerprint and
    calls embed_query_async; a class (not an instance) works because the
    factory's `get_provider()` return value is only attribute-accessed."""

    dim = 768
    fingerprint = "fake:provider:dim=768"

    @staticmethod
    async def embed_query_async(_text: str) -> list[float]:
        return [0.0] * _StubProvider.dim

    @staticmethod
    def embed_query(_text: str) -> list[float]:
        return [0.0] * _StubProvider.dim

    @staticmethod
    def embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.0] * _StubProvider.dim for _ in texts]


class TestPrimaryPath:
    """Primary node goes directly through embedder + milvus, returns relative paths."""

    def test_primary_returns_relative_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Primary path: stub embedder + index → verify returned paths are relative to memory_root."""

        # Use gateway_memory_dir() as the memory root for relative path resolution
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        # create a few fake markdown files in tmp so relative_to doesn't raise
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "foo.md").write_text("x")
        (tmp_path / "bar.md").write_text("y")

        # stub embedder/backend to avoid real Gemini / milvus calls
        import services.memory_indexer.backends.factory as _factory
        import services.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [
                    str(tmp_path / "notes" / "foo.md"),
                    str(tmp_path / "bar.md"),
                ]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "test", "k": 5})
        assert resp.status_code == 200
        # must be relative paths relative to memory_root — must not contain tmp_path prefix
        body = resp.json()
        paths = body["paths"]
        results = body["results"]
        assert paths == ["notes/foo.md", "bar.md"]
        assert len(results) == 2
        assert results[0]["path"] == "notes/foo.md"
        assert results[1]["path"] == "bar.md"
        # description is empty because stub files have no YAML frontmatter
        assert results[0]["description"] == ""
        assert results[1]["description"] == ""

    def test_primary_returns_descriptions_from_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When files have a YAML frontmatter description, results include it."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "with_desc.md").write_text(
            """---
type: Memory
description: A note about the user's health
title: Health Overview
---

# Health
""",
            encoding="utf-8",
        )
        (tmp_path / "no_desc.md").write_text(
            """---
type: Memory
title: No Description
---

# No desc
""",
            encoding="utf-8",
        )
        (tmp_path / "no_frontmatter.md").write_text("# Just a heading\n\nNo YAML.")

        import services.memory_indexer.backends.factory as _factory
        import services.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [
                    str(tmp_path / "with_desc.md"),
                    str(tmp_path / "no_desc.md"),
                    str(tmp_path / "no_frontmatter.md"),
                ]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "test", "k": 3})
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert len(results) == 3
        assert results[0]["path"] == "with_desc.md"
        assert results[0]["description"] == "A note about the user's health"
        assert results[1]["path"] == "no_desc.md"
        assert results[1]["description"] == ""
        assert results[2]["path"] == "no_frontmatter.md"
        assert results[2]["description"] == ""
        # paths still only return paths (backward compat)
        assert body["paths"] == ["with_desc.md", "no_desc.md", "no_frontmatter.md"]

    def test_primary_embedder_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """embedder API failure → IndexerUnavailable (wire 503)."""
        import services.memory_indexer.embeddings.factory as _embedding_factory

        class _BoomProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> Any:
                raise EmbeddingAPIError("gemini quota exhausted")

        monkeypatch.setattr(_embedding_factory, "get_provider", _BoomProvider)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "embed" in body["detail"]

    def test_unexpected_embed_failure_also_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An embed failure that is not an `EmbeddingAPIError` is still an
        outage, not an unmodelled error.

        The embed phase used to catch only `EmbeddingAPIError`, so anything else
        escaped as a bare 500 whose body has no wire `reason` — the SDK cannot
        rebuild `IndexerUnavailable` from that, so a caller that handles the
        outage still saw a raw HTTP error. That is how agent 405 died on
        2026-08-07: the gateway was running out of a deleted worktree's venv and
        the embed client raised `FileNotFoundError` on the missing certifi
        cacert. The milvus phase below already caught broadly; this makes the
        two symmetric.
        """
        import services.memory_indexer.embeddings.factory as _embedding_factory

        class _BoomProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> Any:
                raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(_embedding_factory, "get_provider", _BoomProvider)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"

    def test_primary_backend_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """backend raises (e.g. milvus connect refused) → IndexerUnavailable (wire 503)."""
        import services.memory_indexer.backends.factory as _factory
        import services.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _BoomBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                raise RuntimeError("connection refused 19530")

        monkeypatch.setattr(_factory, "get_backend", _BoomBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"


class TestRequestValidation:
    """Schema validation — query must not be empty, k range."""

    def test_empty_query_rejected(self) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "", "k": 5})
        assert resp.status_code == 422

    def test_k_out_of_range_rejected(self) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 0})
        assert resp.status_code == 422
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 101})
        assert resp.status_code == 422


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


# ── graph grouping vs the type vocabulary ──


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


# ── _extract_meta equivalence (audit #2448 Phase 2) ──


def _legacy_extract_meta(path: Path) -> tuple[str, list[str]]:
    """The pre-#2448 `memory._extract_meta` — reference implementation."""
    import yaml as _yaml

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", []
    if not text.startswith("---\n"):
        return "", []
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return "", []
    try:
        fm = _yaml.safe_load(parts[1])
    except _yaml.YAMLError:
        return "", []
    if not isinstance(fm, dict):
        return "", []
    desc = fm.get("description")  # pyright: ignore[reportUnknownMemberType]
    description = desc.strip() if isinstance(desc, str) and desc.strip() else ""
    raw_tags = fm.get("tags")  # pyright: ignore[reportUnknownMemberType]
    tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
    return description, tags


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


class TestEventLoopIsolation:
    """The embed + milvus calls are synchronous clients; the endpoint must run
    them off the event loop so a slow backend cannot stall the whole gateway
    (the 2026-08-03 freeze: 13 gateway restarts in 8h, three of the five
    examined freezes ended with a gemini-embedding POST as the last MainThread
    log line — the sync call had blocked healthz for minutes)."""

    def test_slow_embed_does_not_block_healthz(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import threading
        import time

        import gateway.routers.memory as _gw_memory
        import services.memory_indexer.backends.factory as _factory
        import services.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "a.md").write_text("---\ntype: Memory\n---\nx\n")

        in_flight = threading.Event()

        class _SlowProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> list[float]:
                in_flight.set()
                await asyncio.sleep(1.0)
                return [0.0] * 768

        monkeypatch.setattr(_embedding_factory, "get_provider", _SlowProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(self, _v: object, _k: int, *, timeout: float) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [str(tmp_path / "a.md")]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            outcome: dict[str, object] = {}

            def _search() -> None:
                outcome["resp"] = client.post("/api/memory/search", json={"query": "x", "k": 1})

            t = threading.Thread(target=_search)
            t.start()
            # Deterministic handshake instead of a fixed sleep: wait until the
            # search request has actually entered the slow embed (audit
            # round-2 cc-docs-tests P2 — a 0.2s sleep could either fire too
            # early under load or waste time when the embed is instant).
            assert in_flight.wait(5.0), "search never reached the slow embed"
            t0 = time.monotonic()
            health = client.get("/api/health")
            health_elapsed = time.monotonic() - t0
            t.join(timeout=5)

        assert health.status_code == 200
        assert health_elapsed < 0.8, (
            f"healthz took {health_elapsed:.2f}s while a memory search was in "
            "flight — the event loop is blocked"
        )
        assert outcome["resp"].status_code == 200  # type: ignore[union-attr]


# --- a stalled backend must degrade the endpoint, not pin it ---

_SEARCH_PERMITS = 2  # the stub's per-test semaphore size (the knob is a setting)
_TEST_DEADLINE_S = 0.5


async def _never_returns(*_args: object, **_kwargs: object) -> list[str]:
    """A backend call that accepts the request and then never answers.

    Takes its arguments loosely on purpose: the point is the handler's
    behaviour around the call, not the call's signature.
    """
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def _stub_search_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    search: Any,
    permits: int = _SEARCH_PERMITS,
) -> asyncio.Semaphore:
    """Point the search handler at a stubbed backend and a short deadline.

    Returns the fresh per-test semaphore so tests can assert on permit state.
    """
    import gateway.routers.memory as _gw_memory
    import services.memory_indexer.backends.factory as _factory
    import services.memory_indexer.embeddings.factory as _embedding_factory
    from shared.config import settings

    monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
    (tmp_path / "a.md").write_text("---\ntype: Memory\n---\nx\n", encoding="utf-8")

    # A fresh semaphore per test. asyncio.Semaphore binds itself to the event
    # loop of its first *contended* acquire and rejects every other loop after
    # that, and pytest-asyncio hands each test its own loop — so sharing the
    # module-level cached object across two contended tests would fail on the
    # second. The handler reads it through `_search_semaphore()`, so the stub
    # replaces that accessor with one returning a single fresh instance.
    fresh = asyncio.Semaphore(permits)
    monkeypatch.setattr(_gw_memory, "_search_semaphore", lambda: fresh)
    monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", _TEST_DEADLINE_S)

    class _StubBackend:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def search_topk_async(self, _v: object, _k: int, *, timeout: float) -> list[str]:
            return await search(_v, _k, timeout=timeout)

    monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)
    monkeypatch.setattr(_factory, "get_backend", _StubBackend)
    return fresh


def _asgi_client() -> httpx.AsyncClient:
    """Drive the app on the *test's* own event loop, so concurrent requests
    contend for the handler's semaphore the way they do in the gateway.
    `TestClient` would run them on its own portal loop instead."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _search(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/memory/search", json={"query": "x", "k": 1})


async def _assert_semaphore_locked(sem: asyncio.Semaphore, timeout_s: float = 5.0) -> None:
    """Wait for concurrent requests to acquire every memory-search permit."""
    deadline = time.monotonic() + timeout_s
    while not sem.locked() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert sem.locked(), "search holders never acquired every permit"


class TestWedgedBackendReleasesPermits:
    """Every search finishes, and its permit comes back — whatever the backend does.

    The evening of 2026-08-03: the handler held one of two permits across an
    unbounded pymilvus await. Both permits were pinned, every later request
    parked in `acquire` with no deadline, and `curl` on the route returned
    neither a response nor an error. Seven agents were stuck in passive recall
    without producing a single LLM turn, and force-killing them only restarted
    the same wait.

    pymilvus is what makes an unbounded await reachable at all: given no
    explicit timeout its retry loop awaits each attempt without
    `asyncio.wait_for`, and connect/close serialize on one process-global lock,
    so a single stalled call outlives the request that made it. These stub that
    away and assert the property the handler owes regardless.
    """

    async def test_wedged_backend_answers_503_instead_of_hanging(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            resp = await asyncio.wait_for(_search(client), timeout=10)

        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"

    async def test_more_wedged_requests_than_permits_all_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The overflow request never reaches the backend — it waits in
        `acquire`, which the deadline has to cover too."""
        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            responses = await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

        assert [r.status_code for r in responses] == [503] * (_SEARCH_PERMITS + 1)

    async def test_a_wedged_embed_is_covered_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The deadline spans both phases, not just the milvus one.

        milvus is the phase with the unbounded pymilvus awaits, but the embed
        runs under the same permit — a deadline covering only the backend that
        happened to stall this time would leave the other phase able to pin the
        endpoint exactly the same way.
        """
        import services.memory_indexer.embeddings.factory as _embedding_factory

        class _StuckProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"
            embed_query_async = _never_returns

        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)
        monkeypatch.setattr(_embedding_factory, "get_provider", _StuckProvider)

        async with _asgi_client() as client:
            responses = await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

        assert [r.status_code for r in responses] == [503] * (_SEARCH_PERMITS + 1)

    async def test_a_cancelled_holder_gives_its_permit_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A client that disconnects mid-search must not cost a permit.

        This is the other candidate mechanism for the outage. uvicorn cancels
        the handler task when the client goes away, and agents were being
        force-killed in batches that day — each kill cancelling whatever search
        that agent had in flight. If a cancelled holder kept its permit, a
        handful of kills would retire every permit and park the endpoint, with
        no thread and no socket left to show for it. That matches the dump
        (14 idle OS threads, nothing on 19530 or 443) exactly as well as a
        stalled backend does, and the recorded evidence cannot separate them —
        a suspended coroutine lives on no thread, so faulthandler cannot see
        which await it stopped at.
        """
        import services.memory_indexer.backends.factory as _factory

        sem = _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            holders = [asyncio.create_task(_search(client)) for _ in range(_SEARCH_PERMITS)]
            # Without this the test could pass vacuously, cancelling requests
            # that had not yet taken a permit.
            await _assert_semaphore_locked(sem)
            for task in holders:
                task.cancel()
            for task in holders:
                with pytest.raises(asyncio.CancelledError):
                    await task

            class _Healthy:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                async def search_topk_async(
                    self, _v: object, _k: int, *, timeout: float
                ) -> list[str]:
                    return [str(tmp_path / "a.md")]

            monkeypatch.setattr(_factory, "get_backend", _Healthy)
            recovered = await asyncio.wait_for(_search(client), timeout=10)

        # A 503 here would mean the permits never came back and this request
        # sat in acquire until its own deadline.
        assert recovered.status_code == 200

    async def test_permits_return_so_a_later_search_still_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Enough wedged requests to pin every permit, then a healthy one."""
        import services.memory_indexer.backends.factory as _factory

        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

            class _Healthy:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                async def search_topk_async(
                    self, _v: object, _k: int, *, timeout: float
                ) -> list[str]:
                    return [str(tmp_path / "a.md")]

            monkeypatch.setattr(_factory, "get_backend", _Healthy)
            recovered = await asyncio.wait_for(_search(client), timeout=10)

        assert recovered.status_code == 200
        assert recovered.json()["paths"] == ["a.md"]


class TestAcquireFastFail:
    """A congested query-embed gate answers 503 in ~1s, not after the search
    deadline (task #2003/E): a fleet wake that saturates the gate should fail
    fast — passive recall's own ~5s deadline then degrades in ~1s, and an
    explicit search learns immediately instead of queueing behind the gate.

    Before this, a deep acquire queue under the (15s) deadline made endpoint
    latency scale with queue length: the 2026-08-29 storm queued 18 searches
    and the recalled agent's first LLM turn waited out the whole queue.
    """

    async def test_congested_gate_fails_fast_with_503(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One permit held by a wedged search; the next request 503s on the
        acquire budget, well before the search deadline."""
        from shared.config import settings

        sem = _stub_search_backend(monkeypatch, tmp_path, search=_never_returns, permits=1)
        # A tiny acquire budget so the failure is provably the fast-fail, and a
        # deadline far above it so the deadline is NOT what answered.
        monkeypatch.setattr(settings.services, "memory_search_acquire_timeout_seconds", 0.05)
        monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", 5.0)

        async with _asgi_client() as client:
            holder = asyncio.create_task(_search(client))
            await _assert_semaphore_locked(sem)
            start = time.monotonic()
            overflow = await asyncio.wait_for(_search(client), timeout=3.0)
            elapsed = time.monotonic() - start
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

        assert overflow.status_code == 503
        assert overflow.json()["reason"] == "indexer_unavailable"
        assert "gate" in overflow.json()["detail"]
        assert elapsed < 1.0, f"congested gate answered in {elapsed:.2f}s, expected ~1s"


class TestSemaphoreCancelSafety:
    """The permit-accounting the handler leans on, pinned rather than assumed.

    `asyncio.Semaphore` has historically been able to lose a permit outright
    when a waiter is cancelled in the window after it has been granted one but
    before it resumes (gh-90155). CPython 3.12 compensates for that in
    `acquire`'s `except CancelledError` branch — `if not fut.cancelled():
    self._value += 1`. These assert the behaviour on the interpreter actually
    running the gateway, so an interpreter change cannot quietly reopen it.

    The handler does not *depend* on this holding: its deadline covers the
    acquire, so a lost permit degrades searches to 503 instead of parking them
    forever. That is the property the outage needed and did not have.
    """

    async def test_cancel_after_grant_does_not_lose_a_permit(self) -> None:
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)  # let it park in the waiter deque
        assert sem.locked()

        # Hand the permit over, then cancel before the waiter can resume: the
        # permit is charged to a task that will never use it.
        sem.release()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # Back in the pool, not stranded on the dead task.
        await asyncio.wait_for(sem.acquire(), timeout=1)

    async def test_cancel_while_parked_does_not_lose_a_permit(self) -> None:
        """The plainer case: cancelled while still waiting, never granted."""
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        sem.release()
        await asyncio.wait_for(sem.acquire(), timeout=1)


def test_semaphore_sized_from_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query-embed concurrency gate reads `memory_search_max_concurrency`
    (env AVA_MEMORY_SEARCH_MAX_CONCURRENCY) — a knob, not a hardcoded constant."""
    import gateway.routers.memory as _gw_memory
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_max_concurrency", 7)
    _gw_memory._search_semaphore.cache_clear()
    try:
        assert _gw_memory._search_semaphore()._value == 7
    finally:
        _gw_memory._search_semaphore.cache_clear()


class TestMemoryNoteEndpoint:
    """GET /api/memory/note — one parsed note by relative path."""

    def test_note_returns_parsed_body_without_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The body is the markdown with the YAML frontmatter stripped, and
        frontmatter values arrive as structured fields."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
description: First note
tags: [ava-internal, tech-ops]
timestamp: '2026-06-18T10:00:00Z'
ava_agent: '7'
ava_machine: test-host
---

# Alpha

Body **with** markdown.
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "alpha.md"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "alpha.md"
        assert body["title"] == "Alpha"
        assert body["description"] == "First note"
        assert body["tags"] == ["ava-internal", "tech-ops"]
        assert body["timestamp"] == "2026-06-18T10:00:00Z"
        assert body["ava_agent"] == "7"
        assert body["ava_machine"] == "test-host"
        # The parser contract: body starts right after the closing fence
        # (a leading blank line is normal for a note's body).
        assert body["body"] == "\n# Alpha\n\nBody **with** markdown.\n"
        assert "---" not in body["body"]

    def test_note_in_subdirectory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Nested notes resolve by their relative path (the graph's node path)."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "health").mkdir()
        (tmp_path / "health" / "overview.md").write_text(
            """---
type: Memory
title: Overview
tags: [health]
---

# Body
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "health/overview.md"})

        assert resp.status_code == 200
        assert resp.json()["path"] == "health/overview.md"
        assert resp.json()["body"] == "\n# Body\n"

    def test_note_missing_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "missing.md"})
        assert resp.status_code == 404

    def test_note_traversal_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Traversal paths are rejected, not resolved — no filesystem leak."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "secret.md").write_text(
            """---
type: Memory
title: Secret
---

# S
""",
            encoding="utf-8",
        )
        # NOTE: no literal "%2F" in `bad` — httpx percent-encodes it again
        # (%252F), so the server sees a plain filename and the test would pass
        # against any implementation (empty pin). "..%2Fsecret.md" on the wire
        # (from "../secret.md") exercises the real encoded-traversal case.
        for bad in ("../secret.md", "/etc/passwd", "alpha"):
            with TestClient(app) as client:
                resp = client.get("/api/memory/note", params={"path": bad})
            assert resp.status_code == 404, bad

    def test_note_without_frontmatter_is_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A .md file that is not a note (no frontmatter) is not a note."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "plain.md").write_text("# Just a heading\n", encoding="utf-8")
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "plain.md"})
        assert resp.status_code == 404

    def test_note_null_byte_path_is_404_not_500(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A path with an embedded null byte must be 404 (QA #1169 F3).

        `Path.resolve()` / `is_file()` raise ValueError on such paths
        (lstat: embedded null character); the endpoint's failure surface must
        map every unresolvable path to 404 rather than escaping as a 500.
        """
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "ok\x00.md"})
        assert resp.status_code == 404

    def test_note_traversal_slash_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`foo.md/../bar.md` resolves inside the root and is a plain read —
        a non-canonical-but-in-root path is legal, only escapes are 404."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "bar.md").write_text(
            """---
type: Memory
title: Bar
tags: [tech-ops]
---

# Bar
""",
            encoding="utf-8",
        )
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "bar.md/../bar.md"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Bar"
