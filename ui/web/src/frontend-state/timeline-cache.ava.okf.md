---
type: doc
title: Selected Timeline State
description: One selected conversation owns the live store and abortable tail/history reads; inactive conversations retain no live buckets.
tags:
- frontend
---

# Selected Timeline State

`/api/system/all?agents=<active>` carries only the selected agent's detailed
events plus system signals. The timeline store holds one selected view.
`switchThread` atomically replaces its identity, items, streaming flags, compact
state, pagination state, and token fields; inactive agents have no live buckets
or compact-marker subscriptions.

The selected timeline query reads one bounded tail on activation. Inactive
queries have zero garbage-collection time. Query cancellation reaches `fetch`
through its AbortSignal. The selected pending-message and token-usage reads
share this selection/visibility ownership. A stream open during an existing
read leaves a trailing read, including the initial no-cache request; leaving
the view disposes that repair before cancelling the HTTP request. Older-page requests have selection-scoped controllers:
a late response after A-to-B-to-A cannot append to the new A view, and cancellation
does not produce an error toast or clear the new view's loading state.

A compact replacement preserves the upstream retention edge: `compactReplaceSeq`
and `compactReplaceAgent` trigger reattachment of the configured previous compact
sessions through the same cancellable history-page read.

History remains fully accessible through explicit paging. Switching releases
loaded inactive history, not durable records. The active view still retains all
pages the user loads; this change does not claim a bound on deep-history memory
or DOM size. Those rendering concerns are independent of subscription ownership.

Timeline snapshots merge with provisional streaming items. The current
wire format does not provide a shared checkpoint revision between REST and live
snapshots; request cancellation alone does not establish commit ordering. An
explicit cross-layer identity contract is required before claiming arbitrary
same-agent snapshot race safety.

## Relationship to Other Nodes

- [[ui/web/src/frontend-state/frontend-state.ava.okf.md|Frontend State Management]]
