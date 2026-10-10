"""Read-only memory search backend reconciliation — queries, two backends, diff.

The pilot tool for the backend switch: run the SAME query texts through
two backends and compare their path orderings. Sample queries come from
the memory pool's frontmatter descriptions; each is embedded once and
searched on both sides. Both backends (numpy / pgvector) are exact, so
their orderings should agree; the diff makes any disagreement visible.

Runs on the gateway box: needs GEMINI_API_KEY (embedding) and both
backends' services up (memory_search daemon / cluster PG).
It opens both backends read-only by default: the indexer daemon's cold-start
reconcile is the only normal writer. `--allow-write` is an intentional
operator escape hatch that requires a second confirmation before connecting.

Usage:
    .venv/bin/python -m services.derived.memory_indexer.memory_search_reconcile --a numpy --b pgvector --limit 50 --k 10
"""

from __future__ import annotations

import argparse
import builtins
import sys

from base.config import ConfigBoot
from base.db import Database
from base.lm.plugin_providers import build_model_catalog
from base.packages.docs.notes import walk_notes
from base.paths import gateway_memory_dir
from services.derived.memory_indexer.backends.base import MemorySearchBackend
from services.derived.memory_indexer.backends.factory import get_backend_named
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.factory import get_provider


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


def _confirm_writable_connect() -> bool:
    """Require an interactive, exact confirmation before enabling writes."""
    if not sys.stdin.isatty():
        print(
            "--allow-write requires an interactive terminal; no backend connected", file=sys.stderr
        )
        return False
    try:
        answer = builtins.input("This may rebuild backend storage. Type exact 'yes' to continue: ")
    except EOFError:
        print(
            "--allow-write confirmation was unavailable (EOF); no backend connected",
            file=sys.stderr,
        )
        return False
    if answer != "yes":
        print("--allow-write was not confirmed; no backend connected", file=sys.stderr)
        return False
    return True


def _connect_backends(
    backend_a: MemorySearchBackend, backend_b: MemorySearchBackend, *, readonly: bool
) -> bool:
    """Connect both backends and report a safe read-only schema refusal."""
    try:
        backend_a.connect()
        backend_b.connect()
    except Exception as exc:
        backend_a.close()
        backend_b.close()
        if readonly:
            print(f"read-only backend connection refused: {exc}", file=sys.stderr)
            print(
                "Run the indexer daemon's cold-start reconcile on startup to repair the "
                "backend, or use --allow-write only if you own that backend.",
                file=sys.stderr,
            )
        else:
            print(f"backend connection failed: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True, help="first backend name (e.g. numpy)")
    parser.add_argument("--b", required=True, help="second backend name (e.g. pgvector)")
    parser.add_argument("--limit", type=int, default=50, help="max sample queries")
    parser.add_argument("--k", type=int, default=10, help="top-k per query")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="allow writable backend connects after typing exact 'yes' interactively",
    )
    args = parser.parse_args()
    readonly = not args.allow_write
    if args.allow_write and not _confirm_writable_connect():
        return 1

    queries = _sample_queries(args.limit)
    if not queries:
        print("no query texts found in the memory pool", file=sys.stderr)
        return 1
    print(f"{len(queries)} sample queries, k={args.k}")

    boot = ConfigBoot()
    boot.boot()

    def api_key() -> str | None:
        value = boot.view.lm.gemini_api_key
        return None if value is None else value.get_secret_value()

    provider = get_provider(
        boot.view.services.embedding_backend,
        catalog=build_model_catalog(),
        timeout_reader=lambda: boot.view.services.memory_embed_timeout_seconds,
        api_key_reader=api_key,
    )
    database = Database.from_settings()
    backend_a = get_backend_named(
        args.a,
        database=database,
        dim=provider.dim,
        fingerprint=provider.fingerprint,
        readonly=readonly,
        uri_reader=lambda: boot.view.services.memory_search_uri,
    )
    backend_b = get_backend_named(
        args.b,
        database=database,
        dim=provider.dim,
        fingerprint=provider.fingerprint,
        readonly=readonly,
        uri_reader=lambda: boot.view.services.memory_search_uri,
    )
    if not _connect_backends(backend_a, backend_b, readonly=readonly):
        return 1
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
            print(
                "sync the backends with the indexer daemon's cold-start reconcile and re-run",
                file=sys.stderr,
            )
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
