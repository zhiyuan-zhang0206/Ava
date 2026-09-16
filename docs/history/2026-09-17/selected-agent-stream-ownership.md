# Selected-agent stream ownership

The owner confirmed that a browser page receives detailed events only for its
currently selected agent. Other agents contribute basic global state changes.
Previously visited conversations are not an independent background-live feature.

We chose one selected live store and activation-time authoritative reads rather
than retaining parked live histories and subscribing to their compaction markers.
This removes lifecycle complexity and prevents resource use from growing with
the number of conversations visited. Switching already resets the viewport to
the latest content; durable history remains pageable after the switch.

Hidden pages suspend their streams without replacement polling. Reopening
reconciles the visible read models. Selection-owned reads abort when abandoned,
including an older-page read during A-to-B-to-A switching. Disposed EventSources
cannot deliver queued callbacks into a newer selection.

This does not establish a total order between existing unversioned checkpoint
reads and in-memory timeline snapshots. That requires a separate contract for
all context-coordinate resets and actual committed checkpoint revisions, not a
browser timing heuristic or a compaction-only counter. Active deep-history
retention/rendering is also separate from inactive subscription ownership.
