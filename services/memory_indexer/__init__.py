"""Memory indexer service.

Long-running daemon watching `~/.ava/memory/` for `*.md` changes — embeds new /
changed files with Gemini Embedding 2 and writes chunk rows (frontmatter
description + body chunks) to the Milvus index. `ava.memory.search` reads the
same index for semantic top-k retrieval.

Layout:
- `daemon.py` — entry, `watchdog` Observer + event queue + cold-start scan
- `healthcheck.py` — pidfile-based liveness probe (matches other service style)
- `embedder.py` — Gemini Embedding 2 client wrapper
- `index.py` — sqlite schema + upsert / delete / top-k search
"""
