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

The selected conversation's three models (timeline / token-usage / pending)
are retained per agent for the 30-minute switch window (`lib/switch-budget.ts`):
a switch back seeds from the retained window and paints immediately, and the
mount/key-change path fires no read of its own. One composed re-attach
reconcile (`agent-reconcile.ts`) refreshes all three: a stream open during an
existing read leaves a trailing composed read (including the initial no-cache
request), a second gap during the composed read leaves one more, and the
composed write never overwrites a newer read. Leaving the view aborts the
in-flight request through its AbortSignal and disposes queued reconcile work,
so query cancellation reaches `fetch`. Older-page requests have selection-scoped controllers:
a late response after A-to-B-to-A cannot append to the new A view, and cancellation
does not produce an error toast or clear the new view's loading state.

A compact replacement preserves the upstream retention edge: `compactReplaceSeq`
and `compactReplaceAgent` trigger reattachment of the configured previous compact
sessions through the same cancellable history-page read.

History remains fully accessible through explicit paging. Switching releases
the inactive store view (scroll-loaded history is not retained); only the tail
window stays cached, for the 30-minute window. Durable records are untouched.
The active view still retains all
pages the user loads; this change does not claim a bound on deep-history memory
or DOM size. Those rendering concerns are independent of subscription ownership.

Timeline snapshots merge with provisional streaming items. The current
wire format does not provide a shared checkpoint revision between REST and live
snapshots; request cancellation alone does not establish commit ordering. An
explicit cross-layer identity contract is required before claiming arbitrary
same-agent snapshot race safety.

## Relationship to Other Nodes

- [[ui/web/src/frontend-state/frontend-state.ava.okf.md|Frontend State Management]]
