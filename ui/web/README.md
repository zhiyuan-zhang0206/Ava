# Ava frontend

The Ava cluster's web UI — Next.js 16 (App Router, Turbopack) + React 19 +
Tailwind 4 + shadcn/ui (Radix primitives). It is the user-facing control
surface for the Ava agent cluster: per-agent chat timelines, the fleet view
(graph / inbox / task board), system control pages (/control), insights and
the memory graph.

## How it talks to the backend

- **SSE, not polling**: two persistent EventSource connections to the
  FastAPI gateway — `/api/system` (global low-frequency broadcast) and
  `/api/system/all` (throttled, batched, every agent's events). The R4 fold
  owner (`src/lib/fold/`) is the single writer folding events into the
  TanStack Query caches; hooks only read their keys.
- No Next rewrites proxy for `/api` — the frontend connects to the gateway
  directly (`API_BASE` resolution in `src/lib/api.ts`); same-origin reverse
  proxy in prod.
- State rules live in `src/frontend-state.ava.okf.md` and
  `src/frontend-data-flow/frontend-data-flow.ava.okf.md`; the R4 concept model is in
  `okf/design/r4-frontend-projection.ava.okf.md` (repo root).

## Development

```bash
npm install
npm run dev        # http://localhost:3000 (gateway expected on :8000)
```

Repo rules (AGENTS.md, repo root) apply — layout-contract classes must go
through `src/lib/layout.ts` primitives (eslint-enforced), and the jsdom +
Playwright layout-invariant layers share `LAYOUT_INVARIANTS`.

## Checks

```bash
npx vitest run    # unit + component tests
npx eslint src    # lint (max-warnings 0)
npx tsc --noEmit  # type check
```
