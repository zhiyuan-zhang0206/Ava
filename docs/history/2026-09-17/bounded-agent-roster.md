# Bound agent discovery by the view being requested

The owner chose a responsive single-page HTTP/1.1 baseline, network independence,
and architectural clarity without compatibility adapters. A production diagnosis
found 65 live agents (45.5 KB) blocked by 6,101 terminated cards (3.96 MB), although
only 22 historical links were required to connect the live tree. The original
opt-in history fetch had been replaced to repair truthful parent relationships.

We chose one coherent live-tree read containing current cards and their unique
ancestor links, an explicit directory page for historical browsing, and separate
ID-addressed selection. Cards carry scalar attention state; notice bodies remain
in their actual detail and Inbox reads. The browser retains one archive page and
cancels abandoned queries. SDK, CLI, MCP, schedules and IM consumers explicitly
choose a scope and cursor; consumers needing enumeration own that loop.

We rejected rewriting provenance to a visible parent because HTTP and lifecycle
events then disagree about spawn/fork facts. We also rejected retaining complete
historical rows in a slimmed payload: it still scales with unrelated history.
A server-rendered tree would couple reads to expansion and presentation; minimal
links preserve the underlying facts while supporting tree and graph views.

Lifecycle events are lightweight invalidation hints. Authoritative reads are
coalesced with guaranteed trailing repair if another hint arrives during a read.
This replaces unversioned snapshot replay, which can replay an older event over
a newer snapshot. Selected detail stays independent of roster membership.

The live response and retained tree scale with live cards plus required ancestors.
Directory pages have a fixed maximum. Actual deep lineage remains a legitimate
cost; unrelated terminated agents and elapsed session time must not enlarge the
live tree. Search may scan directory labels, but card enrichment and response
size stay page-bounded. These are growth invariants, not latency guarantees.

During integration with configurable display defaults, an isolated API probe
showed that an omitted notice limit could return 600 rows when its configured
default was 600, while the same explicit limit was rejected by the 500-row
protective ceiling. A zero default also silently hid an existing backlog.
We applied each consumer's existing lower and upper bounds to the six display
default fields. Boot input and candidate config writes now reject invalid
windows before they reach implicit reads. Defaults remain configurable within
the existing ranges; we rejected clamping invalid configuration or widening
the protective limits to accommodate it.
