---
type: doc
title: "R2 — Single Source of Truth (declaration discipline)"
description: "Concept model (v0.4): one declaration registry per fact family — EnvRegistry (env keys, LANDED), SkillIdentity (skill names, planned), EventSpec (event contracts, planned), resilience.Policy (retry, planned) — collections become derived views, tests become verifiers."
tags:
- design
- single-source-of-truth
---

# R2 — Single Source of Truth (declaration discipline)

> Design lead #2862 · design v0.4 (2026-08-07) · **status: convergence point A LANDED on main (2026-08-06); B/C/D remain planned**

> Landing status (audit round-2 config.md §4): **A (EnvRegistry) is live** —
> `shared/env_registry.py` projections (child_env / env_authority_drop_set /
> env_keep_set) derive from the field registry, and test_env_registry.py +
> test_gateway_consumer_guard.py are derivation-rule verifiers, not snapshot
> seams. Residual: `_DERIVED_FIELDS` / `_IDENTITY_FIELDS` / `_GUIDE_FIELDS` /
> `_HEALTH_PORT_SERVICES` in env_registry.py stay hand-written consumption
> declarations — A3 holds for scope-derived projections, not for those four
> hand sets (test-anchor TODO). B/C/D below are still planned.

## Problem in one sentence

One fact, N handwritten copies, held together by tests and comments: 12 env-key sets hand-copied from a field registry ("env allowlist" leaked six times), one skill name spelled 7 ways, one event contract in 4 copies (rename = repo-wide coordination, #957), 6+ independent retry implementations. Every incident appended a copy at the incident site; the seams where copies drift are exactly where the comments point.

## The core metaphor

> **The registry is the single source of truth; collections are derived views; tests are verifiers, not seams.**

"Tests as seams": `test_profile_env_keys.py` / `test_cluster_env.py` / `test_lint_event_kinds.py` exist only to hold handwritten snapshots against drift — tests patching a structural defect. Final state: a new fact is declared once and every view updates automatically; tests verify derivation rules, not snapshots.

The pattern is uniform across four convergence points, but **the mechanism is independent per domain** — no generic "Registry framework" (that would be over-abstraction).

## Four convergence points

### A. Env keys — `EnvRegistry` (declaration registry, see [[okf/design/design.ava.okf.md|lexicon]])

Moved to [[okf/design/r2-single-source-of-truth/env-registry.ava.okf.md|R2 Env Registry]] — every env key declared exactly once; forwarding/keep-drop/seed-allowlist are pure projections; invariants A1–A3.

### B. Skill names — `SkillIdentity` (an entity, not a string)

A skill's identity is one entity: constructed from any surface (`from_dir`/`from_frontmatter`/`from_cli`), all folding (dash↔underscore) in the constructor, `display_name()` renders. **The install directory is the source of identity; frontmatter `name` is the display declaration** — folded they must be equal, else fail-fast (isomorphic to "cluster identity IS home path"). Registry key = match_key; two directories folding to the same key → `SkillNameCollision` fail-fast. All boundaries accept or produce the entity; bare strings are constructed explicitly at boundaries — forgetting to fold becomes structurally impossible.

Invariants: B1 one skill = one match_key = one directory = one registry row; B2 boundaries only pass `SkillIdentity`; B3 display comes only from frontmatter name (legacy: display = match_key).

Source-sync gap (405 input): repo `.agents/skills/` does not auto-enter the `~/.ava/skills/` registry — a merged skill needs manual `ava skill install`. Final state: visible skill set = registry = union of all managed sources through the identity entity; same-key cross-source conflicts fail fast. (Storage ownership belongs to 02 R1; R2 requires every source through the entity.)

### C. Event contracts — `EventSpec` (one declaration per event)

`shared/events/contract.py` holds `EVENTS: dict[str, EventSpec]` — name × category × payload TypedDict × retention × destination (`events`|`file`) — the single fact source. Writers add one line; producers emit through it (unknown name → fail-fast); the 15 files / 71 `FROM events` read sites consume SQL fragments generated from the TypedDicts (new literal → lint fails); `shared/events/registry.md` becomes a generated artifact. Scope also absorbed: the LLM error family, SSE role lists, and rollup grid constants derive from the registry (add a role = change one place). `sse_drop.kind` is legalized (declared in the payload, zero migration — it is live data).

### D. Retry — `resilience.Policy` (one implementation)

`Policy` (immutable: max_attempts × exponential backoff × jitter (per-process deterministic phase + random span — kills cross-process same-phase) × classify × idempotent × respect_retry_after) + two thin executors `retry`/`aretry` over any callable (not HTTP-bound). Classification: `http_classifier.with(permanent={...})` — override, never rewrite. **Idempotency gating**: `idempotent=False` → single execution + explicit `on_final_failure` compensation hook ("no blind retry, but failure must be visible").

Invariants: D1 exactly one retry loop in the repo (grep-provable); D2 one error-classification semantics, call sites compose overrides; D3 with `idempotent=False`, silent loss is structurally impossible.

## The five global invariants

1. **One fact, one definition** — field ownership / event name+payload+category / skill-identity folding / retry parameters each have exactly one code-level definition.
2. **Derive, don't copy** — collections are computed from the registry at import; a new fact lands in every correct view.
3. **Boundaries pass canonical form only** — `child_env` / `SkillIdentity` / `EventSpec` / `Policy` are the only currency at boundaries; stringly-typed spellings are rejected (fail-fast).
4. **One retry implementation** — classification + backoff + jitter + idempotency gating exist once; call sites declare parameters.
5. **Docs are generated** — registry.md / key tables are generator output, lint-verified.

## Open decision point

- **Q1 — idempotency/delivery semantics**: the same endpoint today has opposite client decisions — SDK `send_message` treats it non-idempotent and never retries (fear of duplicates); IM bridge retries all 5xx (fear of loss). Option A: user rules now, R2 migrates once (recommended); Option B: annotate as-is, ruling deferred to R3's boundary declaration. (Shared with R3 door ①.)

## Related as-is nodes

[[../../../shared/shared.ava.okf.md]] · [[../../../ava/ava.ava.okf.md]] · [[okf/skills/skills.ava.okf.md]]
