# Ava frontend

The Ava cluster's web UI — Next.js 16 (App Router, Turbopack) + React 19 +
Tailwind 4 + shadcn/ui (Radix primitives). It is the user-facing control
surface for the Ava agent cluster: per-agent chat timelines, the fleet view
(graph / inbox / task board), system control pages (/control), insights and
the memory graph.

## How it talks to the backend

- SSE keeps Query read models live for visible pages, with a global stream,
  selected-agent subscriptions and a separate alerts stream. HTTPS can share
  transports across pages; authoritative snapshots repair lifecycle state.
  See [frontend data flow](src/docs/frontend-data-flow/frontend-data-flow.ava.okf.md).
- No Next rewrites proxy for `/api` — the frontend connects to the gateway
  directly (`API_BASE` resolution in `src/lib/api.ts`); same-origin reverse
  proxy in prod.
- State rules live in `src/docs/frontend-state/frontend-state.ava.okf.md` and
  `src/docs/frontend-data-flow/frontend-data-flow.ava.okf.md`; layout and component
  contracts live in `src/docs/frontend-components/frontend-components.ava.okf.md`.

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
npm run lint      # errors and warnings fail; no warning exemptions
npx tsc --noEmit  # type check
npm run build:analyze  # optional local Webpack bundle report
```

Locally, run eslint and vitest on the paths you changed (`npx eslint <files>`,
`npx vitest related --run <files>`) and `tsc --noEmit` once after your last edit;
the project-wide `vitest run` and `npm run lint` belong to CI (see
[the testing guide](../../docs/conventions/testing.md)).
