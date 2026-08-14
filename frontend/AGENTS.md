<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Stack policy

Stay on the shadcn / Radix / Tailwind mainstream. Do not add headless
component libraries outside that ecosystem — the May 25 base-ui saga
(see `decisions/2026-05-25-frontend-radix-stack.md`) cost five PRs
because `@base-ui/react` looked maintained but had not been through
real-iPhone-Chrome shake-out. That decision record carries the rule for
evaluating any future candidate.

# State management

Three mechanisms, one job each: **TanStack Query** = all server data (SSE folds
into the cache, no polling for SSE-backed data) **plus every persistent user
preference**, which is a `user_settings` DB row via `useUserSettings`
(`display.*` / `behavior.*` keys) so it syncs across frontends; **Zustand**
(`lib/store.ts`) = SSE-driven timeline state + client selection (nothing
persisted); **localStorage** = ONLY ephemeral per-device selections (current
mobile tab, last-viewed agent, panel split ratios) — never a durable preference. Don't
mirror server data into Zustand; don't persist a preference to localStorage
(`lib/localstorage-policy.test.ts` enforces the allowlist); one writer per
cache/flag. Full boundaries + the SSE-cache pattern in
[`frontend/src/frontend-state.ava.okf.md`](src/frontend-state.ava.okf.md).
