---
type: doc
title: Gateway memory routes
description: Shared checkout ownership, bounded search and memory graph, note and bundle reads.
tags: [gateway, memory]
---

# Gateway memory routes

The app's `GatewayConfig.memory_root` is the filesystem source for search
reranking, graph, note, bundle and refresh. SDK search results carry relative
paths so a runner can resolve them in its own authoring checkout. The UI graph
uses folder containment as its main structure, with note references layered
on top rather than substituting for the directory tree.

## Read and refresh one checkout

The Git helper's branch is the configured consolidation branch, not necessarily
literal `main`. It returns the updated HEAD; the independent indexer observes
changes afterwards. This endpoint does not await embedding completion. Native
Git work remains under `asyncio.to_thread`; removing that hop can stall the
gateway even though the Git command itself has a timeout.

## Model availability

Search requires the app's complete model catalog before it constructs its
embedding provider and before reranking. Catalog failure retains the gateway's
explicit model-unavailable error, while graph, note, bundle and refresh keep
their existing independent requirements.

## Search budgets

The startup-sized semaphore limits embedding and backend phases, not reranking. Acquires have their own short congestion budget, and the total search deadline also covers waiting for permits. Timeout or cancellation releases acquired permits. Candidate budgets depend on purpose, not on requested k or exclusions.

The embedding provider's dimension and fingerprint identify the backend vector
space even on reads. Keeping each await bounded prevents a stalled provider
from consuming every permit indefinitely; a shorter acquire budget lets a
congested caller degrade promptly. The enclosing deadline and disconnect path
also cancel outstanding ranking work. An existing IndexerUnavailable is not
rewrapped as a different backend failure.

## Graph and file safety

The graph includes recursive notes, their ancestor folders and containment edges; an empty pool has no nodes. Reserved index/log/MEMORY files and dangling references are excluded. Grouping prefers a domain tag over type/*, preserving domain structure rather than collapsing notes into type buckets.

The app-local graph cache is keyed by checkout path and Git HEAD; failed or absent revision reads bypass caching. The revision command uses fixed Git argv without a shell, with a bounded wait and failure fallback.

Keeping several revisions avoids immediate eviction around consecutive refreshes.
The cached model is shared for serialization and callers must not mutate it.
This revision marker does not track arbitrary uncommitted file edits.

Bundles carry committed ancestry and the advertised HEAD, excluding untracked
machine-local clutter. This lets a fresh split runner join the consolidated
history rather than creating unrelated initial commits. OS/timeout failures
are translated at the route boundary; Git failures retain their dedicated
HTTP error. Route descriptions remain native FastAPI `description` values;
moving docstrings to that runtime field preserves the existing OpenAPI output.

Parent: [[gateway/routers/docs/routers.meiva.okf.md]].
