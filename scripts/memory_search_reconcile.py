"""Memory search backend reconciliation — same queries, two backends, diff.

The pilot tool for the backend switch: run the SAME query texts through
two backends and compare their path orderings. Sample queries come from
the memory pool's frontmatter descriptions; each is embedded once and
searched on both sides. Exact backends (numpy / pgvector) agree with each
other; an approximate one (milvus) may legitimately reorder near-ties —
the diff makes exactly that visible.

Runs on the gateway box: needs GEMINI_API_KEY (embedding) and both
backends' services up (milvus daemon / memory_search daemon / cluster PG).

Usage:
    .venv/bin/python scripts/memory_search_reconcile.py --a milvus --b numpy --limit 50 --k 10
"""

from __future__ import annotations

import argparse
import sys

from services.memory_indexer.backends.factory import get_backend_named
from services.memory_indexer.embeddings.base import EmbeddingAPIError
from services.memory_indexer.embeddings.factory import get_provider
from shared.notes import walk_notes
from shared.paths import gateway_memory_dir


def _sample_queries(limit: int) -> list[str]:
    """Frontmatter descriptions from the memory pool, newest-first, capped at
    `limit` — the short entity-bearing lines the index exists to find."""
    queries: list[str] = []
    for _, note in walk_notes(gateway_memory_dir()):
        if note.description and note.description.strip():
            queries.append(note.description.strip())
        if len(queries) >= limit:
            break
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True, help="first backend name (e.g. milvus)")
    parser.add_argument("--b", required=True, help="second backend name (e.g. numpy)")
    parser.add_argument("--limit", type=int, default=50, help="max sample queries")
    parser.add_argument("--k", type=int, default=10, help="top-k per query")
    args = parser.parse_args()

    queries = _sample_queries(args.limit)
    if not queries:
        print("no query texts found in the memory pool", file=sys.stderr)
        return 1
    print(f"{len(queries)} sample queries, k={args.k}")

    provider = get_provider()
    backend_a = get_backend_named(args.a, dim=provider.dim, fingerprint=provider.fingerprint)
    backend_b = get_backend_named(args.b, dim=provider.dim, fingerprint=provider.fingerprint)
    backend_a.connect()
    backend_b.connect()
    try:
        meta_a = backend_a.all_meta()
        meta_b = backend_b.all_meta()
        if set(meta_a) != set(meta_b):
            # A stale / unsynced backend makes the ordering diff meaningless
            # (a path missing on one side cannot be compared at all) — report
            # the row-set gap and stop instead of printing misleading diffs.
            only_a = sorted(set(meta_a) - set(meta_b))
            only_b = sorted(set(meta_b) - set(meta_a))
            print(
                f"row sets differ: {args.a}={len(meta_a)} paths, {args.b}={len(meta_b)}; "
                f"only in {args.a}: {only_a[:5]}",
                file=sys.stderr,
            )
            print(f"only in {args.b}: {only_b[:5]}", file=sys.stderr)
            print("sync the backends (cold-start reconcile) and re-run", file=sys.stderr)
            return 2
        exact = 0
        for i, text in enumerate(queries, start=1):
            try:
                vector = provider.embed_query(text)
            except EmbeddingAPIError as exc:
                print(f"[{i}] embed failed, skipping: {exc}", file=sys.stderr)
                continue
            paths_a = backend_a.search_topk(vector, args.k)
            paths_b = backend_b.search_topk(vector, args.k)
            if paths_a == paths_b:
                exact += 1
                continue
            print(f"[{i}] {text[:60]!r}")
            print(f"    {args.a:>10}: {paths_a}")
            print(f"    {args.b:>10}: {paths_b}")
        print(
            f"summary: {exact}/{len(queries)} queries identical ({args.a} vs {args.b}, k={args.k})"
        )
    finally:
        backend_a.close()
        backend_b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
