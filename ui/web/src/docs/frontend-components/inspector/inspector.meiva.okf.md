---
type: doc
title: Inspector Panel
description: The right-side panel for the selected agent — one ordered section list, the responsive close surfaces and the render-nothing-while-closed boundary.
tags: [frontend]
---

# Inspector Panel

`components/inspector/inspector-panel.tsx` — the single-agent counterpart of the
sidebar's fleet-wide stats card. Only the selected agent is read, only while the
panel is open; current state, window statistics and plugin widgets load and fail
independently ([[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]]),
and each section's own rendering facts live in
[[ui/web/src/docs/frontend-components/inspector/section-details.meiva.okf.md|section details]].

**One ordered list, no filter.** The body is a single list of sections
(`INSPECT_SECTION_ORDER` in `lib/inspector/inspector-widgets.ts`): the Open run
timeline entry (50) first, the merged Cost + Activity section (75) second, the
Context section (90), then the runtime sections in their established order —
page (100), shells (200), liveness (300), configuration overlay (400) — and the
open notice (800) last.
Plugin widgets carry their own `order` and slot between those keys; equal orders
stack built-in sections first, then widgets by (plugin, id), so the sort is
deterministic regardless of registration order and DOM order is visual order.
The non-embedded header carries the panel title alone (no section/category
selector); `embedded` panels render no header at all — the mobile close X below
has its own rule.

**Empty sections render nothing** — a hidden Page section with no open page, an
available-but-empty shell list, an empty config overlay, a
missing notice, a widget whose payload came back empty. The first-load skeleton
promises only the built-in sections that may appear: the notice and plugin-widget
areas are not promised by it (plugins have their own generic placeholder).

**Responsive (user ruling 2026-08-23, superseding the 2026-08-05 floating-overlay
ruling on desktop).** At ≥ lg the panel fills the resizable right-side panel;
below lg it is a full-screen overlay with a backdrop, matching the mobile sidebar
drawer. At ≥ lg the top-bar InspectorToggle is the panel's one close control
(user ruling 2026-08-24 — a header X here duplicated its "Close inspector"
affordance, QA sweep 2026-09-18 F2); below lg the overlay header X closes it (the
overlay covers the toggle, so the X is the only reachable close) and the backdrop
closes it too. Escape deliberately does not close either form (user ruling
2026-08-24), and clicking outside the desktop panel does not close it either.

**Rendering while closed.** Closing renders nothing — desktop releases the
flex-column width, mobile removes the overlay — while all hooks still run
(rules-of-hooks) with the queries disabled, so a closed panel cannot produce
inspect traffic. Closing aborts the in-flight reads.

**Agent switch.** The panel stays mounted across agent switches (the page resets
its error boundary via `resetKey`, not `key=`), so each switch also restores the
scroll-to-top reset the former remount gave a new selection.

```meiva-contract
id = "section-order"
kind = "invariant"
statement = "The panel renders one merged sorted list: the Open run timeline entry (50) first, the merged Cost + Activity section (75), the Context section (90), then page (100), shells (200), liveness (300) and the configuration overlay (400), and the open notice (800) last. Plugin widgets keep their own order and slot between those keys; ties stack built-in sections first, then widgets by (plugin, id). The non-embedded header carries the title alone — no category or section filter — and embedded panels render no header."
code = [
  "ui/web/src/lib/inspector/inspector-widgets.ts:INSPECT_SECTION_ORDER",
  "ui/web/src/components/inspector/inspector-panel.tsx:InspectorPanel",
]
tests = [
  "ui/web/src/components/inspector/inspector-panel.test.tsx::InspectorPanel section layout::renders the run timeline entry first, Cost + Activity second, then the runtime sections",
  "ui/web/src/components/inspector/inspector-panel.test.tsx::InspectorPanel section layout::embedded: renders without the header bar or the category selector",
  "ui/web/src/components/inspector/inspector-panel.test.tsx::InspectorPanel plugin widgets::slots a widget into the section order (750: between the configuration overlay and the notice)",
]
```

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
