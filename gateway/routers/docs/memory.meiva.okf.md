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

```meiva-contract
id = "refresh-owner"
kind = "invariant"
statement = "Refresh updates the same captured memory_root that the app reads. Its keep-local and track-branch values are read from the existing process configuration for each request. Git runs off the ASGI event loop so another request can complete while a refresh waits."
code = ["gateway/routers/memory.py:post_memory_refresh"]
tests = ["gateway/tests/routers/memory_search/test_memory_refresh.py::test_refresh_updates_the_checkout_read_by_the_app", "gateway/tests/routers/memory_search/test_memory_refresh.py::test_refresh_keeps_serving_while_git_waits"]
```

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

```meiva-contract
id = "bounded-search"
kind = "invariant"
statement = "The startup-sized semaphore limits embedding and backend phases, not reranking. Acquires have their own short congestion budget, and the total search deadline also covers waiting for permits. Timeout or cancellation releases acquired permits. Candidate budgets depend on purpose, not on requested k or exclusions."
code = ["gateway/routers/memory.py:MemorySearchInputs", "gateway/routers/memory.py:build_search_gate", "gateway/routers/memory.py:_bounded_semaphore", "gateway/routers/memory.py:_backend_topk", "gateway/routers/memory.py:post_memory_search", "gateway/routers/memory.py:_vector_candidates", "gateway/routers/memory.py:_candidate_count"]
tests = ["gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestWedgedBackendReleasesPermits::test_wedged_backend_answers_503_instead_of_hanging", "gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestAcquireFastFail::test_congested_gate_fails_fast_with_503", "gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestSemaphoreCancelSafety::test_cancel_after_grant_does_not_lose_a_permit", "gateway/tests/routers/memory_search/test_search_budget.py::test_decision_requests_do_not_grow_with_the_exclusion_list", "gateway/tests/routers/memory_search/test_search_budget.py::test_excluded_notes_never_reach_the_decision_model_or_the_response", "gateway/tests/routers/memory_search/test_search_budget.py::test_dedup_ranks_its_own_smaller_candidate_count_whatever_k_is"]
```

The embedding provider's dimension and fingerprint identify the backend vector
space even on reads. Keeping each await bounded prevents a stalled provider
from consuming every permit indefinitely; a shorter acquire budget lets a
congested caller degrade promptly. The enclosing deadline and disconnect path
also cancel outstanding ranking work. An existing IndexerUnavailable is not
rewrapped as a different backend failure.

## Graph and file safety

```meiva-contract
id = "graph-projection"
kind = "invariant"
statement = "The graph includes recursive notes, their ancestor folders and containment edges; an empty pool has no nodes. Reserved index/log/MEMORY files and dangling references are excluded. Grouping prefers a domain tag over type/*, preserving domain structure rather than collapsing notes into type buckets."
code = ["gateway/routers/memory.py:_build_memory_graph", "gateway/routers/memory.py:_folder_structure", "gateway/routers/memory.py:primary_tag"]
tests = ["gateway/tests/routers/memory_search/test_memory_graph.py::TestMemoryGraph::test_graph_nested_folder_tree_gets_one_pseudo_node_each", "gateway/tests/routers/memory_search/test_memory_graph.py::TestMemoryGraph::test_graph_empty_pool_has_no_nodes_or_edges", "gateway/tests/routers/memory_search/test_memory_graph.py::TestMemoryGraph::test_graph_skips_reserved_names", "gateway/tests/routers/memory_search/test_memory_graph.py::TestMemoryGraph::test_graph_dangling_edges_are_filtered", "gateway/tests/routers/memory_search/test_memory_graph.py::test_primary_tag_prefers_a_domain_tag_over_the_type_tag"]
```

```meiva-contract
id = "graph-revision"
kind = "invariant"
statement = "The app-local graph cache is keyed by checkout path and Git HEAD; failed or absent revision reads bypass caching. The revision command uses fixed Git argv without a shell, with a bounded wait and failure fallback."
code = ["gateway/routers/memory.py:_pool_revision", "gateway/routers/memory.py:MemoryGraphCache", "gateway/routers/memory.py:get_memory_graph"]
tests = ["gateway/tests/routers/test_memory_graph.py::test_graph_builds_once_per_pool_revision", "gateway/tests/routers/test_memory_graph.py::test_graph_uncached_without_a_git_revision"]
```

Keeping several revisions avoids immediate eviction around consecutive refreshes.
The cached model is shared for serialization and callers must not mutate it.
This revision marker does not track arbitrary uncommitted file edits.

```meiva-contract
id = "note-containment"
kind = "invariant"
statement = "A note path must resolve inside memory_root to a readable Markdown note. Absolute paths, traversal, null bytes, missing files and invalid frontmatter share the 404 surface rather than exposing filesystem errors."
code = ["gateway/routers/memory.py:get_memory_note"]
tests = ["gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestMemoryNoteEndpoint::test_note_traversal_returns_404", "gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestMemoryNoteEndpoint::test_note_null_byte_path_is_404_not_500", "gateway/tests/routers/memory_search/test_event_loop_isolation.py::TestMemoryNoteEndpoint::test_note_without_frontmatter_is_404"]
```

Bundles carry committed ancestry and the advertised HEAD, excluding untracked
machine-local clutter. This lets a fresh split runner join the consolidated
history rather than creating unrelated initial commits. OS/timeout failures
are translated at the route boundary; Git failures retain their dedicated
HTTP error. Route descriptions remain native FastAPI `description` values;
moving docstrings to that runtime field preserves the existing OpenAPI output.

Parent: [[gateway/routers/docs/routers.meiva.okf.md]].
