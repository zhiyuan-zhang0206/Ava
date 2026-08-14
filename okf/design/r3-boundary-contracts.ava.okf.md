---
type: doc
title: "R3 — Boundary Contracts (doorplates on doors)"
description: "Planned concept model (v0.3, awaiting user): every cross-process boundary is a door with a doorplate declaring idempotency, pause exemption, and truth source — five doors (API calls, pause exemption, pages, notices, skills). Final state of a Big Bang migration."
tags:
- design
- planned
- contracts
- cross-process
---

# R3 — Boundary Contracts (doorplates on doors)

> Design lead #2863 · design concept v0.3 (2026-08-07) · **design-phase node — the current system is NOT this.**

## Problem in one sentence

Six kinds of cross-process interaction (HTTP API, DB direct read/write, session tree, env passing, event table, SSE) each carry their rules only in comments and caller goodwill: one endpoint gets three different client answers on idempotency; pause exemptions are string special-cases in middleware (three incidents, three patches); a page's state lives in three places (two volatile); the notice panel reads three pipes; four consumers each scan the skill tree independently. Every incident = one more special case at the incident site.

## The core metaphor

> **Every cross-process boundary is a door. Each door carries a doorplate stating three things: can it be pushed again (idempotency), is it open during migration (pause exemption), who is responsible for it (truth source). A doorplate is code, not a comment — consumers inherit behavior from the doorplate instead of guessing.**

One metaphor, one new concept (the rest are existing ideas re-declared at boundaries).

## The five doors

| Door (deliverable) | Doorplate content | Who reads it |
|---|---|---|
| **① API calls — idempotency/delivery** | `idempotency`: `Idempotent` (safe to retry) / `NonIdempotent` (no auto-retry) / `AtLeastOnceWithKey` (server-side dedup, safe to retry) + `pause`: `control-plane` (exempt) / `data-plane` (default, 503 during migration) | middleware (decision function), SDK clients (inherit retry policy), tests (lint forces every route to declare) |
| **② Pause exemption** | Exemption = a route-declared attribute, no more middleware strings | middleware consumes only the tested decision function `should_bypass_pause(route)`; the exempt surface is enumerable and auditable |
| **③ Pages — truth source + supervisor** | Truth source = the `agent_pages` table; supervisor = an independent daemon (spawns/kills per DB row, **not** on the session tree, **not** dependent on the agent heartbeat); `serve()` materializes content to disk | page daemon, gateway reverse proxy |
| **④ Notices — single data source** | Truth source = the `agent_notices` table; unified write API (atomic); unified aggregate read endpoint; TTL/expiry policy | frontend (single hook), IM bridge (direct DB read, decoupled from pause) |
| **⑤ Skills — materialized index** | `SkillIndex.build()` — a pure builder (lint and runtime share one implementation); mtime-invalidated cache | every skill read path (query the index, never scan) |

## Who writes, who reads

| Declaration / entity | Writer | Reader | Supervisor |
|---|---|---|---|
| Contract constants (`shared/contracts.py`) | route author | middleware, SDK, lint | tests |
| `agent_pages` table | `serve()` declares, daemon updates state | daemon (spawn/kill), gateway | page daemon |
| `agent_notices` table | unified notice API | frontend aggregate endpoint, IM bridge | TTL maintenance |
| `SkillIndex` | `build()` | skill read paths | mtime invalidation |

## The three invariants

1. **Declared at the boundary definition** — idempotency / exemption / truth source are declared at the entry definition (contract constants + route reference); copying into callers, special-case tables, or comments is forbidden.
2. **Server promises, clients inherit** — callers derive behavior (retry/dedup/exemption) from the contract; guessing is forbidden.
3. **Single truth source + decoupled supervisor** — every cross-process entity has exactly one truth source (page = table, notice = table, skill = index); in-process state is only a cache; resource liveness never depends on the host process.

## Explicit handoffs (no gaps)

- **With R2 (skill identity)**: identity definition (fold at construction, canonical at render) = R2's `SkillIdentity`; `SkillIndex.build()` consumes that entity as the index key — materialization = R3.
- **With R2 (idempotency)**: the doorplate declares the semantic; R2's `resilience.Policy.idempotent` is the client-side execution parameter. Shared decision point (R3 门① / R2 Q1).
- **With R1 (pause)**: R1 owns pause *state* (`host_deploy_state.posture`); R3 owns the *exemption policy* (which routes survive a migration, per-route, test-backed).
- **With R1 (exit self-report)**: leases make liveness independent of self-report (R1); self-reports that remain are contractually declared (R3).
- **With R4**: SSE fold/notice frontend work is R4's; R3 ends at the read endpoints.

## What it does NOT do

No deployment-state model (R1) · no event contracts / skill identity / env-key registries (R2) · no SSE fold layer / layout (R4) · no new infrastructure: no message bus; the page daemon reuses the watchdog/restarter pattern; the idempotency dedup table generalizes the existing `cluster_rpc` mechanism into one shared table.

## Open decision points

- **Q1 — physical location of contract declarations**: shared contract module (`shared/contracts.py`, recommended — precondition of invariant 2, one fact source) vs pure route decorators (declaration gateway-side only, SDK relies on doc discipline — today's comment-contract).
- **Q2 — IM bridge pause decoupling**: direct DB read (recommended — existing daemon pattern: watchdog/heartbeat read the DB; the gateway-uniform-RPC principle governs gateway↔runner RPC, not daemon↔DB) vs gateway exempt route (everything through the gateway, but one more exempt surface + notice consumption lives and dies with gateway availability).

## Related as-is nodes

[[../../gateway/gateway.ava.okf.md]] · [[../../services/services.ava.okf.md]] · [[../../shared/shared.ava.okf.md]]
