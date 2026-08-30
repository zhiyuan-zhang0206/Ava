"""Memory indexer service.

Long-running daemon watching `~/.ava/memory/` for `*.md` changes — embeds new /
changed files through the configured embedding provider (Gemini Embedding 2 by
default) and writes chunk rows (frontmatter description + body chunks) to the
configured storage backend. `ava.memory.search` reads the same index for
semantic top-k retrieval.

Layout:
- `daemon.py` — entry, `watchdog` Observer + event queue + cold-start scan
- `embeddings/` — provider contract (`base.py`), Gemini adapter (`gemini.py`),
  factory switch (`factory.py`, `AVA_EMBEDDING_BACKEND`)
- `backends/` — storage backends (milvus / numpy / pgvector) behind the
  `MemorySearchBackend` protocol + factory switch `AVA_MEMORY_SEARCH_BACKEND`
"""
