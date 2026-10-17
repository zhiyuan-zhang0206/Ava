# Continuous conversation flow and directional loading

Status: partial implementation accepted in preview, updated 2026-10-15. The creator
reviewed the real frontend with frozen checkpoints in an OrbStack Docker preview.
Compact has no separator or replacement boundary UI. The broader retention and
request-lifecycle findings below remain follow-up work. This document does not
describe a production deployment.

## Accepted preview scope

- Earlier history uses a circular pull indicator, with content following the
  gesture. A released pull requests one page; prepend preserves the reading anchor.
- Warm selection restores each agent's last reading position, loaded window and
  expansion choices. Refreshing the latest messages does not resume bottom following.
- The bottom button keeps its clickable down arrow while a loading ring rotates
  around its edge. Clicking resumes following even during a refresh.
- Human and agent reply titles remain in flow. Expanded work blocks and their
  expanded detail headers retain their two sticky levels, suppressed during fast
  scrolling.
- Compact separators and their reserved spacing are removed.

Current behavior is owned by the
[timeline view](../ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md)
and [timeline cache](../ui/web/src/docs/frontend-state/timeline-cache.meiva.okf.md).
The original audit findings in the review evidence remain open: this preview does
not replace the Compact retention pipeline, add read deadlines or repair shared
SSE connection-generation replay. Physical macOS trackpad release timing still
needs hands-on verification in Chrome; automated browser checks cover native
wheel paging and held touch gestures.

## Scope

Prevent persistent history spinners, repeated refresh feedback and loss of
previously displayed conversation rows after Compact. The creator and developers
familiar with agents read multiple agents over long sessions. The desired tone
is quiet, restrained and reliable.

The creator's requirements are:

- Earlier messages load at the top of the conversation.
- Opening or catching up the latest messages is shown at the bottom, using a
  different visual treatment.
- Compact is a small event/boundary inside a continuous conversation. It must
  not clear or replace the displayed message list.
- Old messages remain readable, and updates preserve the reading position.

This work concerns conversation presentation and its request lifecycle. It does
not change the model's need to compact its working context. Bounded DOM rendering
remains necessary for long histories. History visibility cannot depend on an
automatic page budget or the success of a canonical-history prefetch.

## Prior art

Use TanStack Query for server reads, Zustand for the selected conversation's live
display, the existing retained-checkpoint history APIs, and the current bounded
timeline renderer. Directional pagination and immutable message identity are
established messaging patterns. Each request has one owner and an explicit
settlement; a transport state replay is not a new connection opening.

## Simplest sufficient design

Keep one selected conversation display composed of committed message segments
and its live tail. Prepend history, append new messages and insert Compact
events. Reconcile a runtime snapshot into its matching segment. Do not use a
post-compact runtime context as a replacement for the entire conversation.

Reuse the retained boundary checkpoints and existing durable source coordinates
(`source_message_id`, `source_inbound_id`, `source_block_idx`) where they cover the
rendered rows. Audit their coverage before choosing the complete identity
contract. Current-segment positional IDs are reused after Compact, and historical
rank changes at later compacts: neither is a permanent display identity. Missing
source identity and provisional streaming rows need an explicit identity bridge;
do not invent timestamp-based deduplication or claim arbitrary REST/SSE ordering
is safe without a shared segment/revision contract.

This approach needs no new history database or general event replay framework.
Its unresolved implementation boundary is proving segment identity and revision
ordering for live snapshots and retained pages; the current wire format does not
provide the whole proof. Establish the smallest owner contract before replacing
the current swap/buffer pipeline.

## Review evidence

The reviewed frontend files are identical between the primary checkout and
isolated branch base `e4ad41160a978d49e3feac30b05aca294ed4404e`.
Four existing suites passed: 142 tests covering `useTimeline`, the shared SSE
transport, `useEventStream` and the conversation reconcile coordinator.
Additional isolated probes expose cases absent from that baseline:

| Priority | Finding | Evidence and consequence |
|---|---|---|
| P1 | A compact discovered during history paging leaves loading latched | Both SSE and HTTP replacement advance `compactEpoch`; the old page is rejected by `loadOlderSegment`, but `loadingOlder` remains true. Two probes fail the expected settlement. The top spinner persists and further history reads are blocked. |
| P1 | Finite compact retention deletes displayed history | `finishCompactTransition` drops uncovered buffered rows after the page budget settles. Retention zero skips preservation entirely. Existing tests explicitly assert these behaviors, contrary to the creator's current requirement. |
| P1 | Conversation reads have no client deadline | Both history and composed refresh remain pending after 300 seconds of simulated time, whether fetch or response-body consumption hangs. Four probes confirm the missing bound. Retries cannot begin while a promise never settles. |
| P2 | Agent switching replays a previous detail connection's open state | A transport probe records `open -> reconnecting -> open` when changing the selected union. The cached first `open` predates subscription to the newly selected agent, yet readers treat it as a fresh gap repair. |
| P2 | New followers replay open to existing pages | A transport probe records an extra `open` for an already-open follower when another page joins. The leader broadcasts state for newcomer discovery without a target or connection-generation deduplication. Existing readers schedule unnecessary repair. |

Code locations:

- [History request ownership](../ui/web/src/lib/timeline/use-timeline.ts),
  especially the epoch checks and early aborted returns in `loadOlderSegment`.
- [Compact display replacement](../ui/web/src/lib/timeline/timeline-store-events.ts)
  and [store cleanup](../ui/web/src/lib/timeline/timeline-store.ts).
- [Budget-driven cleanup](../ui/web/src/lib/timeline/use-compact-history-retention.ts).
- [Unbounded conversation reads](../ui/web/src/lib/transport/api.ts).
- [Cached and broadcast open states](../ui/web/src/lib/transport/sse-share.ts).

These are code and isolated lifecycle reproductions. The reported Chrome visual
flicker was not reproduced in a browser recording. The floating header's backdrop
blur and epoch-dependent row keys are further repaint/remount suspects, not
confirmed root causes. Removing animation alone would leave the lifecycle bugs.

## Directional loading design and remaining work

The table includes proposed failure handling and cold-load feedback beyond the
accepted preview scope. Latest catch-up uses the implemented arrow and edge ring.

| Operation | Placement and feedback | Completion and failure |
|---|---|---|
| Earlier page | A quiet circular spinner revealed by a native pull, with the message stream following the gesture | Prepend once; preserve the visible message's offset. A failure becomes a quiet inline retry at the same boundary. Exhaustion becomes static "Beginning of conversation". |
| Cold agent selection | Bottom-aligned static message-shaped placeholder and "Opening conversation" | Display only the selected agent's results. Cache hits paint immediately without a skeleton. Failure shows a bottom retry. |
| Latest catch-up | Rotating ring on the edge of the enabled scroll-to-bottom button; its down arrow remains visible | Reconcile while preserving the bookmark. Clicking resumes following. Empty success removes the ring normally. |
| Agent work | In the agent's current work/message block | Follow authoritative work lifecycle; do not equate a running agent with an HTTP loading request. |
| Connection recovery | Stable text in a fixed header slot | A connection generation drives one repair, not every replayed `open`. No header pulsing or title replacement. |

Top and bottom operations are independent and may coexist. Each tracks its own
request token, selection owner and deadline, not a shared loading boolean.

Delay visual feedback by approximately 200 ms to avoid flashing for fast reads.
This is a display threshold, not a minimum loading duration: settlement cancels
the pending indicator immediately. Slower reads can change the copy in place;
they must eventually report a failed or unavailable read rather than spinning
forever. Choose the deadline against the owning endpoint's measured budget;
35 seconds is the existing bounded-read precedent, not a newly proven SLO.

Cancellation cannot settle a successor request. A changed epoch retires the old
owner and its flag together. Loss of selection or visibility aborts obsolete
reads. Loading success, empty success, failure, cancellation and supersession
are all explicit terminal paths.

## Compact presentation

The creator clarified on 2026-10-14 that Compact is an internal event and may
have no dedicated presentation at all. Remove the existing separator and its
reserved gap. Do not add a replacement divider, timeline marker, summary link,
or persistent event card. The reader cares about continuity of the process.
Preserve original messages across Compact and keep its UI inconspicuous.

The earlier standalone interaction mockups were rejected and do not establish
an accepted design. The next review uses the actual frontend in an OrbStack
Docker preview cluster, with frozen real checkpoints copied into its isolated
data plane. The creator will give feedback while reviewing that frontend.

## Alternatives considered

1. Retain the current replacement and remove only the spinner. Rejected: finite
   restoration can still remove messages, and the same ownership races remain.
2. Keep the display snapshot continuous and fetch older canonical identity on
   demand. Preferred: preserve what the reader already saw, while history reads
   only add coverage. Canonical handover must retain message identity and content.
3. Build a new persistent conversation event log. Deferred: the retained history
   and source coordinates already cover much of the requirement. Add storage only
   if their coverage audit identifies a concrete unsolved persistence boundary.

## Implementation and acceptance

First establish stable display/segment identity and request ownership, then remove
the cleanup transitions and add directional UI. Update the meaning or retire
`display.compact_history_sessions`: it may control prefetch, never permission to
erase history. DOM virtualization may unmount offscreen nodes while preserving
logical messages and exact history accessibility.

Required regression coverage:

- Both observed epoch-change history races release the request and indicator.
- Zero, finite and unlimited prefetch settings preserve all displayed old rows.
- Two consecutive compacts retain old messages without dedicated separators.
- REST/SSE reorder, retry, empty pages and terminal paging do not reset the list.
- Agent switches, hide/show and newcomer tabs trigger at most one repair per
  actual subscription generation; stale cached open cannot certify a new agent.
- Reading and expansion survive prepend, Compact and canonical identity handover.
- Cold and warm selection use bottom feedback correctly; top loading is never
  used for a new-tail read. Each deadline covers fetch and body consumption.
- Browser frame sampling in Chrome verifies stable header and reading position;
  a steady completed page has no remaining loading animation.

The redesign increases selected-conversation memory retention compared with the
old Compact cleanup. Keep the bounded DOM renderer and measure heap growth with
long histories. A memory/window policy must preserve logical continuity and
reliable paging; it must not become an implicit Compact deletion policy.
