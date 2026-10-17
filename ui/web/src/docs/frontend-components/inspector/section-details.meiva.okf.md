---
type: doc
title: Inspector section details
description: What each Inspector section renders — cost/activity metrics and their formatting, shells, pages, the config-overlay diff, liveness cells, the notice reply surface and the no-fabrication rule.
tags: [frontend]
---

# Inspector section details

Rendering facts of the panel's sections; the shell (layout, order, close
surfaces) is in [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md|Inspector Panel]]
and the reads behind them in
[[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]].

**Open run timeline row.** The top entry is a link to `/insights/run/{agentId}`
that wears the same row chrome as the Page section's rows — the rounded
`bg-sidebar-accent/40` block, a leading external-link icon that brightens on
hover, and the hover highlight — so the panel's clickable rows read as one
family. The panel's amber stale dot renders at its end — it marks
"live refresh is failing" only when a snapshot is already displayed
(stale-while-error); a cold failure gets the full error message in the body
instead, so the dot never replaces content. The sibling links (`fleetTaskHref`,
`fleetNoticeHref`) are typed literal routes so `next/link` infers them without a
cast.

**Cost + Activity** is one section with two grids. Cost: total `$` (four
decimals) with an `N unpriced` sub-line when calls without a stored usage-time
price snapshot exist — those contribute 0 and are surfaced rather than hidden,
never silently partial; LLM calls; tokens in / out combined; cache-hit percent
(`N/A` when unmetered). Activity: TPS (the LLM stage's), then the absolute
durations the header window covers — LLM output, code execution, and idle
(`alive − active`). Without life (`alive_seconds = 0`) the three duration cells
show em dashes while TPS stays visible. Durations use the shared tiering —
`45s` / `15m` / `1h` / `1h 30m` / `24d 3h` (day tier past 24h) — and TPS renders
one decimal, `0` → em dash. Token counts roll `≥1000M` to `B` and past `1000B` to
`T`. The section header carries the window picker and the ⟳ ([[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]]).

**Page** lists the agent's open pages as external links (title or name plus the
URL), hidden entirely when none is open; the read is `useAgentPages`.

**Persistent shells.** The badge shows the shell count, or `?` when observation
is unavailable — an unavailable observation must not render as an empty set. A
row links to its full-screen monitor page `/shell/{agentId}/{shellId}` and shows
only the ticking runtime (launch → now, computed per second from `created_at`;
falling back to the probe-time `uptime_seconds` snapshot when `created_at` is
missing). The created/TTL detail deliberately lives on the monitor page's title
bar, not here (user corrections 2026-08-28). A row whose agent or shell id is
not a valid finite integer renders as plain text, so the panel never navigates
to `/shell/NaN/NaN`.

**Configuration overlay.** Rows render every overlay entry's value — strings
pass through, everything else is JSON-stringified. Skill-list keys
(`skills_to_inject_into_system_prompt`, `skills_to_expand_at_start`) are stored
in the runtime's underscore Python projection and render in the canonical dash
spelling (the same rule as the `# Capabilities` index); `*` stays `*`.

**Liveness** (one merged section, user rulings Task #1195 / 2026-08-12 and
2026-09-18 field order, Task #3952 / #3904). Cells in display order: agent birth
— `spawned_at` (the true birth, written once at spawn), rendered as
"relative, absolute" on a single line (user ruling 2026-08-24) and deliberately
not `started_at`, which refreshes on every restart; next heartbeat — both
projected instants (the end of an active pause and the check-in time) render
through `formatUpcoming`, so the day boundary is part of the value: clock time
later today, `Tomorrow 3:21 PM` across local midnight, a weekday name two to six
days out, a date countdown from seven days, and a plain date from a month on. A
bare clock time is never shown for a future day — `3:21 PM` read at 4:26 PM
looks like a time that already passed (user report 2026-10-17). A queued
check-in renders "pending" (the daemon will not send another while an inbound is
pending), a running or terminated agent an em dash, and a projected time already
in the past renders "due" (a past "*next*" time must never read "Xm ago"); last
pause (relative time · duration, or "never paused"); then machine and lifecycle
status. The status cell reuses the fleet's active-lease projection so an
impersonated agent reads "Impersonated" while its native lifecycle stays idling;
a terminated panel keeps "Terminated" even if a stale roster card is cached. The
gateway's derived liveness state colors the heart icon when offline. The "every
N" interval badge, the old "Last judged" cell and all observation details stay
omitted.

**Notice** renders the agent's single open notice at the bottom as the
interactive reply surface (a `require_response` notice gets a reply box +
Dismiss, an FYI gets Mark read; the same numbered question controls as the
timeline), with a jump link into the fleet inbox. `OpenNoticeDetail` keys by
notice id: a resolve leaves `pending` true while the notice is going away, and
the keyed remount gives the *next* notice a fresh, enabled reply surface.
Resolving invalidates the live query so the notice clears without waiting for
the interval.

**Widgets.** Plugin widgets render through the shared section chrome
(`inspector-section.tsx`), so plugin sections and built-in ones share one header
component; the widget section itself renders only the closed-set kinds the
console knows (`InspectWidgetSection`) and a widgets fetch failure leaves the
rest of the panel intact.

**No fabrication.** The panel renders the metrics, or nothing: coverage
verdicts, duration precision and retained-source notes are machinery that go to
the background log and alert episodes instead of the UI (user ruling 2026-09-17,
task #3869); unavailable metrics never become zero cost, zero throughput or a
verdict, and the fleet Alerts block is not part of the inspector (user ruling
2026-08-29). Formatting helpers are shared (`formatTokens`, `formatRelative` /
`formatAbsolute` / `formatShort` / `formatUpcoming` / `formatUptime`), so tests
assert against the shared helper's own output rather than re-deriving it. The
day-context table this section quotes is owned by
[[ui/web/src/lib/format/docs/time.meiva.okf.md|Timestamp display]].

Parent: [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md]].
