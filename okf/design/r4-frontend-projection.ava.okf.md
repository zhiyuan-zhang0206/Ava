---
type: doc
title: "R4 — Frontend Projection (layout + state layers)"
description: "Planned concept model (v0.6, awaiting user): the frontend is a projection of one server truth line — a fold layer centralizes snapshot×stream reconciliation, panels declare one data contract, layout gets named invariants with a two-layer test defense, breakpoints unify responsive design. Final state of a Big Bang migration."
tags:
- design
- planned
- frontend
---

# R4 — Frontend Projection (layout + state layers)

> Design lead #2864 · design concept v0.6 (2026-08-07) · **design-phase node — the current system is NOT this.**

## Problem in one sentence

Two broken things: **layout** has no engine-level test defense (the same div failed twice in 48h as P0s: #874 vertical, #979 horizontal — jsdom cannot render `display:flex`/`min-width:auto`), and **realtime state folding** is re-implemented per hook — 9 hooks each hand-roll the same snapshot×SSE reconciliation, each inventing its own (possibly wrong) race handling (8/7 still producing new incidents).

## The core metaphor

> **The frontend is a projection surface of one server truth line.** HTTP snapshot = baseline, SSE events = deltas, both describing the same server state. All client state is a projection of that line: folding is a pure function, UI is derived. Every race (event-before-response, disconnect-window loss, compact rewriting history, thread-switch interleave) is **one** question — "how do two truth lines reconcile" — solved once in the fold layer, not nine times.

## The four layers

### Layer 1 — Fold layer (`lib/fold/`) — the heart

- **Event** — one SSE message, grouped by class (agent / timeline / notice / graph / page / usage / pending-message).
- **Reducer** — `(state, event) -> state`, pure, unit-testable, one file per event class.
- **Reducer chain** — `applyEvent(state, event)`: dispatch by event class; the **single entry point** of all folding.
- **Merge protocol** — the snapshot×stream reconciliation semantics as a reusable reducer primitive (baseline merge, pending markers, reconnect invalidation); today's scattered race bits (4 state flags in timeline-store, the spawn "three-piece", per-hook invalidate) all converge into its fields/steps.
- **Derived state** — fold output; components/hooks only subscribe.

Ownership: `EventStreamProvider` holds the fold instance — events feed `applyEvent`, per-domain output lands via `queryClient.setQueryData` into that domain's Query key; **hooks stay as they are** (`useQuery` on their own key) but no longer write folding or reconciliation. No hook invents reconciliation ever again. The timeline hot path keeps its separate store (measured performance constraint — high-frequency folding notifies timeline subscribers only) but draws its race semantics from the same merge protocol, not a second implementation.

### Layer 2 — Data contract layer (one panel = one contract)

**Panel** — a self-contained UI block (Inbox, agent list, task graph…); **data contract** — the panel declares "where my data comes from, what happens on failure". Inbox final state: **one data source** (the notice endpoint family, Q1), deleting the snapshot dependency and the client-side three-pipe merge. Error strategy unified: default stale-while-error (keep last data + "may be stale" badge), critical paths (agent list / notices) add toast — 7 treatments converge to 2, silent failure structurally gone (the user could not distinguish "queue empty" from "load failed").

### Layer 3 — Layout contract layer

- **Layout invariants (`LAYOUT_INVARIANTS`)** — named, testable layout contracts, I1–I6 (no horizontal page scroll; surface not wider than parent; composer doesn't overflow; fleet toolbar doesn't widen; inbox rows don't overflow; single main scroll region). Three viewport tiers: 320 / 390 (#979 precedent) / 768.
- **Layout primitives** — flex contracts (`min-w-0`/`min-h-0`/`display:flex`) promoted from bare classNames to shared primitives/constants; eslint forbids raw writing.
- **Two-layer defense** — jsdom class-contract (millisecond, full CI) + Playwright real-engine measurement (I1–I6 at three tiers). The timeline-page false-green gap (SSE-fed page can't connect in a fresh browser context) is closed with an injected fake EventSource (`add_init_script` replacing `window.EventSource`, pushing a deterministic event sequence — snapshot → delta → reconnect; `page.route` cannot stream to an EventSource).

Layout redesign goes from "edit classNames in 6 files" to "change one primitive's prop", under a test net.

### Layer 4 — Breakpoint layer

**`useBreakpoint()`** — the single breakpoint source (320/390/768/lg); **`useInspectorOpen()`** — the single interface dispatching per breakpoint (desktop = DB-persisted setting, mobile = session-volatile; #793's deliberate semantics preserved, mechanism hidden). Three responsive mechanisms → one (breakpoint + conditional render); dual-component always-mounted pattern deleted; `style={{display:'none'}}` tab-switching deleted.

## The five invariants

1. **Single truth line** — one pipe per data domain; folding centralized and pure; new views never invent reconciliation.
2. **Two-layer layout defense** — every layout invariant = jsdom contract + engine assertion; 320/390/768 tiers.
3. **Errors visible** — "failed" and "empty" always distinguishable; default stale-while-error, no silent failure.
4. **Breakpoint single point** — responsive decisions go through the one breakpoint abstraction; no second mobile implementation.
5. **One data contract per panel** — one declared data source per panel; no hand-merged multi-pipe.

## Line-budget discipline (design constraint)

The whole-repo outlier-zeroing ruling (2026-08-07) applies to the R4 domain: every file the design touches must land ≤600 soft / 800 hard (today: agent-sidebar 1122, timeline/index 935, inbox-queue 796, timeline-store 696, use-agents 613). If a refactored file still exceeds the limit, the design split is incomplete — back to the concept model, not an exemption.

## Open decision points

- **Q1 — notice data contract shape**: A (recommended): backend merges `GET /api/notices` returning `{open, awaiting, resolved_page, next_cursor}` — one panel = one request = one hook (touches the gateway API; consumers IM bridge/CLI/tests to check); B: frontend single-hook convergence over the existing three queries (no API change, still 3 requests + client merge).
- **Q2 — e2e defense strength**: A (recommended): Playwright layer 2 mandatory in CI (a defense that doesn't run isn't a defense — two P0s in 48h); B: local/post-deploy manual runs.

Implementation shape (already decided, not a decision point): final-state design, four sequential big PRs (layout+test infra → notices → breakpoints → fold layer), each PR lands as the terminal state, no intermediate compatibility layer; all green then one-shot deploy.

## Related as-is nodes

[[../../ui/web/web.ava.okf.md]] · [[ui/web/src/frontend-state.ava.okf.md]]
