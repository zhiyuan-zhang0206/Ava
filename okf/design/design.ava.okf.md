---
type: doc
title: Design Concepts (R1–R5)
description: "Concept models from the 2026-08-07 architecture audit (4 root design problems R1–R4, each with a final-state concept model; R5 in design). Design-phase nodes describe the target state, not the current system; since 2026-08-08 pieces land on main in slices — each node carries its own landed status."
tags:
- design
- planned
---

# Design Concepts (R1–R5)

## What this domain is

The audit Round 1 (2026-08-07) traced 48h of incidents to **four root design problems** spanning seven subsystems. Each root got a design concept document (by design leads #2861–#2864), and — per user ruling — the new concept models are consolidated here, into the OKF, for the user's own review.

**These nodes describe the planned final state, not the current system.** Every other `.ava.okf.md` node in this graph describes what the system **is**; this domain describes what it is designed to **become** (Big Bang migration, user-ruled 2026-08-07). The design leads are still iterating (each document below carries its own open decision points); when a design lands, this domain is reconciled against the code and the concepts migrate into the as-is domain docs.

**Landed status (2026-08-08)**: the migration is slice-wise, not Big Bang — R1–R4 have all started landing on `main` (see the Status column below). A node whose row says "partially landed" is a mix: read the row and the node's own banner, then trust the **code** over the design prose for anything that is in flight.

## The five roots

| Root | Problem | Concept node | Status |
|---|---|---|---|
| **R1** | State / liveness has no explicit model — "what is happening now" is an implicit conjunction of signals | [[okf/design/r1-state-liveness.ava.okf.md]] | Design v3.3 · **partially landed**: `host_deploy_state` + `deployment_state` table, agent/updater leases on main |
| **R2** | Single-source-of-truth discipline missing — every incident appended a copy of the logic at the incident site | [[okf/design/r2-single-source-of-truth.ava.okf.md]] | Design v0.4 · **partially landed**: `env_registry.py`, `SkillIdentity` (`shared/skill_names.py`), `EventSpec` registry (`shared/events/contract.py`) on main; retry convergence still in flight |
| **R3** | Cross-process boundary contracts have no owner — contracts survive on comments and goodwill | [[okf/design/r3-boundary-contracts.ava.okf.md]] | Design v0.3 · **partially landed**: `contracts.py`, `skill_index.py`, page-server supervision, notices unified write API on main; doorplate enforcement still in flight |
| **R4** | Frontend: layout contract has zero enforcement; realtime fold layer is a cognitive single point | [[okf/design/r4-frontend-projection.ava.okf.md]] | Design v0.6 · **partially landed**: fold layer (`frontend/src/lib/fold/`) on main; layout invariants / two-layer defense still in flight |
| **R5** | Skill / MCP / plugin install & update | — (design lead #2884 in progress) | **In design — not written up** |

## Shared vocabulary (one concept, one definition)

The four designs independently coined overlapping terms. This is the consolidated lexicon — the definition lives here once, each design node uses it:

- **单一事实源 / single source of truth** — one fact has exactly one authoritative definition or store. The principle applies at three layers, one per design: **state** (R1: one authority storage + one read API + one set of legal transitions), **declaration** (R2: one code-level definition per fact; collections are derived views), **entity** (R3: each cross-process entity has one truth source; in-process state is only a cache). R1's 思想 1, R2's 唯一事实源, R3's 单真相源 are the same principle at these three layers — one term, three instances.
- **注册表 / registry** — two distinct meanings, kept apart: (a) **entity registry** (R1) — a persistent row recording that a managed entity *should exist* (who / what type / schedule / state); answers "should-exist". (b) **declaration registry** (R2) — the one code location where a fact is declared (env field, event spec, skill identity), from which all views derive. R3's skill index is the materialized read-side of R2's identity entity.
- **租约 / lease** — "I am alive" promise: the holder periodically renews; expiry = dead. Already exists today as the `cluster_update_lock` TTL; R1 generalizes it to every liveness question. Voluntary self-report ("I quit the lease") only accelerates convergence, never required.
- **真相源 / truth source** (R3) — the single authoritative store for a cross-process entity (a DB table, a built index).
- **投影 / projection** (R4) — the frontend is a projection of one server truth line (HTTP snapshot = baseline, SSE events = deltas); all client state is derived from folding that line.
- **门牌 / doorplate** (R3) — the declaration hung on each cross-process boundary (a door): idempotency, pause exemption, truth source. A doorplate is code, not a comment.
- **折叠 / fold** (R4) — pure function (old state + event → new state); the single reconciliation of snapshot × event stream, centralized instead of re-implemented per hook.

## Cross-design handoffs (explicit, no gaps)

- **R2 ↔ R3 (skill identity)**: identity definition (fold at construction, canonical at render) belongs to R2's `SkillIdentity`; R3's `SkillIndex.build()` consumes that entity as index key — materialization belongs to R3.
- **R2 ↔ R3 (idempotency)**: R3 declares idempotency at the API boundary (service-side promise, `idempotency` field on the doorplate); R2's `resilience.Policy.idempotent` is the client-side execution parameter. One shared decision point (R2 Q1 / R3 门①): same question, two perspectives.
- **R1 ↔ R3 (pause)**: R1 owns the pause *state* (`host_deploy_state.posture`); R3 owns the pause *exemption policy* (which routes survive a migration, declared per-route with tests).
- **R1 ↔ R3 (exit self-report)**: R1 replaces liveness dependence on the exit-notify HTTP chain with leases; R3 guarantees that self-reports which remain (e.g. `/exited`) are contractually declared.

## Open decision points (user sign-off required)

| Design | Question | Lead's recommendation |
|---|---|---|
| R1 Q1 | State storage shape: two tables vs single table + JSONB hosts column | Two tables |
| R1 Q2 | Deployment phase enumeration: three states vs five | Three states (`stable`/`updating`/`settling`) |
| R2 Q1 | Idempotency/delivery semantics (SDK non-idempotent no-retry vs IM bridge retry-all-5xx) | User decides now, or defer to R3 |
| R3 Q1 | Physical location of contract declarations: shared contract module vs route decorators | Shared module (`shared/contracts.py`) |
| R3 Q2 | IM bridge pause decoupling: direct DB read vs gateway exempt route | Direct DB read |
| R4 Q1 | Notice data contract shape: merged `GET /api/notices` vs frontend single-hook convergence | Merged endpoint |
| R4 Q2 | e2e defense strength: Playwright in CI mandatory vs manual | In CI |

## Related as-is nodes

Current-system descriptions these designs will change: [[cli/cli.ava.okf.md]] (orchestration/update legs) · [[gateway/gateway.ava.okf.md]] (pause middleware, API surface) · [[shared/shared.ava.okf.md]] (env keys, events, telemetry) · [[frontend/frontend.ava.okf.md]] · [[agent/agent.ava.okf.md]] (lifecycle) · [[services/services.ava.okf.md]] (daemons).
