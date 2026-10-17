---
type: doc
title: Frontend telemetry
description: "One-way interaction telemetry: client buffer and caps, batched POST /api/frontend-telemetry, gateway validation, and the instrumented points."
tags: [frontend]
---

# Frontend telemetry

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value, dedupe})` → in-memory buffer (dedupe 2s per page/element/key; Web Vitals and API timing opt out; under the 100/min per-tab and 200 pending caps) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → the unified event stream (Loki) → Grafana core panels.

Instrumented points: page views plus native FCP/final LCP/CLS/INP (`lib/telemetry-page-view.tsx` + `lib/web-vitals.ts`, mounted in AuthGuard's authenticated branch), API requests slower than 800ms (`lib/api.ts`, normalized numeric path segments), composer send-to-first-turn-start latency (`lib/interaction-timing.ts`), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), every user_settings change (`lib/use-user-settings.ts` setSetting), and one `sse-transport` event per authenticated tab session (`key=shared|fallback`, value carries secure-context/Web Locks/BroadcastChannel flags). No sensitive content: `element` is a closed union, keys are bounded normalized identifiers, and `value` is a ≤64-char scalar.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md|Frontend Data Flow]] — the node this one-way flow departs from.
