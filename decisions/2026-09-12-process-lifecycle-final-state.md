# Process/service lifecycle final state: two-section root bootstrap, one full tree, one-shot migration

## Context

The runtime currently couples macOS-proprietary machinery into general
semantics: the permissions helper and TCC attribution share one axis with
cross-platform mechanism code (dual spawn backends, the
`AVA_PERMISSIONS_HELPER_SPAWN` switch, a half-migrated per-process backend
choice). The user's 2026-09-12 order asked for the architectural decoupling
itself, not a point fix, plus a full-tree lifecycle end-state; the three-step
skeleton came the same day at 16:08. Draft-1 went through #1818 fact
verification (today every chain root is detached) and #405 review; the user
ruled the open decision points on 2026-09-12 17:40–17:41. Design record:
[`future/infra/lifecycle-final-state.md`](../future/infra/lifecycle-final-state.md).

## Decision

1. **Two-section bootstrap, per platform.** macOS: launchd starts
   `permission-helper` (the seeder), and the helper **pulls up root** — two
   independent programs/codebases; the helper keeps the permission-execution
   surface and never doubles as root. Linux/WSL/Windows: the system service
   manager pulls up root directly; the helper is not involved. **Hard
   constraint: zero permission content in root code**, enforced at the code
   layer by a file/symbol-level lint rule whose violations block merge.
2. **One root supervisor (`ava-root`) owns one full process tree per
   (machine × $AVA_HOME)** — all long-lived processes (services, agent host,
   session/PTY hosts) and turn children; the chain never truncates
   ("attribution tree = process tree"); root is single-instance, never
   restarts for upgrades (exec replacement), and is the whole tree's reaper.
3. **Full tree scope.** "Sovereign / no teardown can reach it" protections
   are replaced by a semantic survive-update marker plus observability;
   `_reparent`'s double fork is retired; lifecycle actions are semanticized
   per unit type.
4. **Availability fallback: lose attribution, not service.** On a root/helper
   crash, existing units keep running while spawn/management pauses
   (degraded); the OS adapter restarts root; recovery reseeds the
   attribution-requiring subchains. Adoption (management takeover without
   re-parenting) is documented as the design's exception state — POSIX
   cannot re-attach chains to existing processes; and (F1/F2, 2026-09-12)
   no reparent-family trigger resets attribution while the chain root
   lives (16 scenarios enumerated; static at spawn),
   narrowing the actual loss case to the chain root's own lifecycle events
   (helper death/restart; F12 to measure).
5. **Supervision collapses from 4–5 layers to 2**: the OS adapter keeps root
   alive; root keeps the whole tree alive (parallel pull-up + startup gating,
   self-healing units; no declarative dependency DAG).
6. **One-shot (big-bang) migration** per the 2026-08-07 precedent; the
   per-Mac user touchpoint is one ~1–2 minute authorization click, not
   repeated by any canary-then-final sequence.

## Alternatives rejected

1. **helper-as-root fusion** (the design group's original recommendation; the
   helper is already nine-tenths of a root service). Rejected by the user:
   the general core must stay OS-pure — fusion puts generic runtime code
   inside a macOS-proprietary process and rebuilds the coupling the redesign
   exists to remove. The two-section form keeps two independent
   code/keepalive chains, with the seeder as the only macOS-specific item.
2. **Runtime-only tree** (only services + agent host in the tree; PTY/exec
   stay detached). Leaves chain integrity with break points and process
   ownership conceptually dangling; rejected for the full tree, where
   protective isolation becomes semantic markers + observability.
3. **Declarative dependency DAG** (explicit depends_on, topological startup).
   Introduces a DAG concept and scheduling state, drifts toward a fat
   manifest, and conflicts with the deliberately minimal unit manifest;
   self-healing + startup gating wins on Keep It Simple.
4. **"root down = kill the whole tree"** (lose service, keep attribution).
   Attribution consistency is not a security boundary (user ruling
   2026-08-10); killing the tree is a hard service interruption that does not
   recover attribution any faster; a regression against today's detached
   independent survival.
5. **Run the helper-spawn canary first** (recorded trade-off, not a rejection
   of the end state). The final-state path does not require it — it is a
   half-migrated state; whether a canary runs during the transition is an
   independent transition-period decision (presented to the user separately),
   and either way the user authorization happens once.

## Consequences

- **Two independent programs/codebases** (permission-helper/seeder +
  ava-root) with isolated failure domains; the permission-execution surface
  stays entirely in the helper, and the zero-permission lint becomes a merge
  gate for the root codebase.
- **Measurement debt accepted; F1 first.** The design rests on TCC
  attribution propagating across the full hierarchy; F1 (the multi-level
  attribution probe across the whole two-section tree) ran 2026-09-12 —
  full-chain attribution held (27/27); see the design record's F1 result.
  F2 (reparent enumeration) likewise ran: no trigger resets the anchor.
- **Migration preconditions**: complete company-air's inventory before the
  cutover; the per-machine window is booked with the user directly, and the
  user watches the cutover live.
- **Per-Mac user touchpoint**: one ~1–2 minute authorization click (System
  Settings), identical whether a transition canary lands or the final state
  lands first — never cumulative.
