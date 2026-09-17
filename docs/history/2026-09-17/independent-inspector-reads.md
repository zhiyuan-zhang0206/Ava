# Independent Inspector reads

The owner selected independent section loading on 2026-09-17, superseding the
2026-09-12 coordinated first reveal recorded in PR #2312. A slow historical
query must not hide available current state or plugin content.

The chosen boundary is current state, window statistics, and plugin extensions.
The mixed statistics endpoint repeated current-state reads and runner probes
that the browser discarded. It was replaced rather than retained as a
compatibility adapter; in-repository clients and fixtures move together.

Two alternatives were rejected: preserving a global loading barrier keeps the
slowest dependency on every section's critical path; merging everything into
one response preserves overlapping ownership and makes selective refresh harder.

Speculative row prefetch was removed. It consumed limited HTTP/1.1 request
capacity for unselected agents and let pointer-leave cancellation compete with
the selected panel's shared query. Only the selected panel owns these reads;
inactive Inspector cache entries are discarded rather than accumulating visits.
Notice/task events refresh their own current domains. Statistics reconcile on
selection, compact, manual refresh and a 60-second visible-panel interval, so
network reconnect storms cannot continually restart historical computation.

This change does not redefine metrics, assert complete historical coverage, or
replace historical aggregation. Shared server computation retains bounded
admission/deadlines after an individual browser waiter disconnects; cancellation
of queued sections prevents expired leaders from retaining unnecessary work.

The remaining current-state dependency on a Loki last-pause lookup was removed
using the existing heartbeat_pause_log trail. Its writer records each pause in
the same transaction as the control-plane deadline, and its latest-per-agent
index already supports the read. The existing 24-hour display horizon remains;
no duplicate projection or new collection pipeline was introduced.
