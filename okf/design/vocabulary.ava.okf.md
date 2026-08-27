---
type: doc
title: Design Shared Vocabulary
description: The consolidated lexicon of the four design roots — one concept, one definition (single source of truth / registry / lease / doorplate / projection / fold).
tags:
- okf-design
---

# Shared Vocabulary

## Shared vocabulary (one concept, one definition)

The four designs independently coined overlapping terms. This is the consolidated lexicon — the definition lives here once, each design node uses it:

- **single source of truth** — one fact has exactly one authoritative definition or store. The principle applies at three layers, one per design: **state** (R1: one authority storage + one read API + one set of legal transitions), **declaration** (R2: one code-level definition per fact; collections are derived views), **entity** (R3: each cross-process entity has one truth source; in-process state is only a cache). R1's state authority, R2's one-definition-per-fact, and R3's single truth source are the same principle at these three layers — one term, three instances.
- **registry** — two distinct meanings, kept apart: (a) **entity registry** (R1) — a persistent row recording that a managed entity *should exist* (who / what type / schedule / state); answers "should-exist". (b) **declaration registry** (R2) — the one code location where a fact is declared (env field, event spec, skill identity), from which all views derive. R3's skill index is the materialized read-side of R2's identity entity.
- **lease** — "I am alive" promise: the holder periodically renews; expiry = dead. Already exists today as the `cluster_update_lock` TTL; R1 generalizes it to every liveness question. Voluntary self-report ("I quit the lease") only accelerates convergence, never required.
- **truth source** (R3) — the single authoritative store for a cross-process entity (a DB table, a built index).
- **projection** (R4) — the frontend is a projection of one server truth line (HTTP snapshot = baseline, SSE events = deltas); all client state is derived from folding that line.
- **doorplate** (R3) — the declaration hung on each cross-process boundary (a door): idempotency, pause exemption, truth source. A doorplate is code, not a comment.
- **fold** (R4) — pure function (old state + event → new state); the single reconciliation of snapshot × event stream, centralized instead of re-implemented per hook.


Parent: [[okf/design/design.ava.okf.md|design domain]].
