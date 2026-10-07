# CLI ↔ gateway boundary: dedup by boundary, not by habit

## Context

A capability can be reachable from both the CLI and the gateway. Where overlap is
real, the question is how to dedup: have one call the other, or have both import a
shared implementation. Most CLI commands — host/process bootstrap
(`start`/`stop`/`install`/`converge`/migrations, …) — have no gateway form and
can't: a gateway can't bring itself up. The genuine overlap was a handful of
hand-written DB queries in the CLI duplicating logic the gateway already owned
(machine listing, agent quiesce). Constraints: non-update CLI commands must stay
fast, which depends on a deliberate lazy-import boundary; and `if TYPE_CHECKING`
is banned repo-wide, so type-only imports can't be hidden.

## Decision

One implementation per capability, living in the lowest layer all callers import
(`shared.*`). REST routers are thin transport for remote/browser callers. The CLI
imports the shared function directly for co-located ops, and acts as a thin
HTTP/RPC client only when the call genuinely crosses a machine. The dedup
mechanism is chosen by the **boundary** — in-process import vs HTTP/RPC — not by
habit.

Concretely: the shared `SELECT FROM machines` predicate moved to
`shared.machines.list_agent_hosts()`; the bulk agent-quiesce SQL (inbound restart
signal + live-agent status queries) moved to `shared.db`, with the CLI keeping
only the poll loop and timeout. CLI wrappers survive only as test seams.

## Alternatives rejected

- **Make the CLI call its own gateway over HTTP for co-located ops.** Wrong fix
  for an in-process call — adds network, serialization, and a running-gateway
  dependency to reach code already importable in the same process. HTTP is for
  crossing a machine boundary, not for dedup.
- **Add a shared op-kind registry.** The op-kind vocabulary is already
  centralized as a single `OpKind` `Literal` — not three copies. Both consumers
  (the agent-host dispatcher and the CLI's path→kind map) already fail loud on an
  unknown value, so a forgotten wire-up surfaces the first time it's exercised;
  there is no silent-drift bug for a registry to prevent. A registry would be
  more machinery than the existing invariant (one `OpKind` + fail-loud) already
  buys. Landed as cross-reference comments only.
- **Statically type the CLI's path→kind map against `OpKind`.** Rejected: it
  forces a top-level gateway-layer import into the CLI, defeating the lazy-import
  that keeps non-update commands fast — and the type-only import can't be hidden
  behind `if TYPE_CHECKING`, which is banned.
- **Restructure the API toward path-based selection (`timeline/{id}`).** The API
  is already RESTful and path-addressed. The `?agent_id=…` query param is the
  single-page app's client-side selection state, not an API design — query-param
  is the honest model for "which row is selected in one shell". Path-based
  routing would add layout/remount machinery for no functional gain.

## Consequences

- A capability has exactly one home, in the lowest layer importing it; routers
  and the CLI are thin over it.
- Adding a cross-machine op still touches the dispatcher and the CLI map, but a
  miss fails loud on first use rather than drifting silently — the cost of not
  having a registry, accepted because fail-loud already covers it.
- The lazy-import fast path for non-update CLI commands is preserved by not
  pulling gateway-layer types into the CLI.
- Underlying principle: use the strongest invariant that already kills the
  ambiguity, rather than adding machinery to manage it.
