# Fail-fast vs reconcile: the boundary

## Context

Core philosophy mandates fail-fast, yet the ops line kept growing self-heal
gates (schema, pin drift, stranded pause, redis ACL) — each added after an
incident, each a structured fallback. Unstated, the two postures looked
contradictory, and self-healing was growing by incident folklore rather than
by rule.

## Decision

One criterion: **look for a learner in the causal chain of the error.**

1. **Model mistakes** (bad code, wrong enum, hallucinated API) → fail fast in
   the turn. The model is in the loop; a shim steals its learning signal.
   Sole exemption: strippable shims in the plugin quarantine
   (`ava_syntax_fix`), which must themselves fail fast internally
   (compile-guard: if the fix isn't provably safe, return the original).
2. **Operator/config mistakes** → fail fast at the boundary (boot/spawn
   preflight; the `validate_model_config` 400 pattern). The learner is human.
3. **Our own bugs** (broken contracts between subsystems) → fail fast, never
   self-heal. A reconciler papering over our bug produces the "lied about
   update completion" incident class: symptom cured, cause immortal.
4. **World drift** (process died, ACL vanished, pin drifted, schema behind)
   → reconcile toward spec. No learner is in the loop; a crash converts
   drift into outage. This is self-healing's only legitimate jurisdiction.

Supporting rules:

- **No spec, no self-heal**: every reconciler must point at an explicit state
  dimension in the ops Spec; the state-dimension inventory is the admission
  gate for new self-heal behavior.
- **Healing must be loud**: every heal logs and surfaces in status; repeated
  healing of the same dimension in a short window escalates to an alert
  (crashloop-backoff analogy) — chronic drift means there is a bug and, per
  rule 3, someone should be woken up.
- **Retries are bounded micro-reconciles**: transient upstream errors may
  retry with backoff, but only under a fatal classification cap (the
  `FatalProviderError` pattern), or retry degenerates into an unbounded shim.

## Alternatives rejected

- Self-healing our own bugs (symptom masking; see incident class above).
- Silent self-heals (kin of the false-green probe).
- Unbounded retry without a fatal taxonomy.

## Consequences

Philosophy corollaries 5–6 added to `docs/current/philosophy.md`; reconciler
admission is governed by the ops Spec inventory
(`future/infra/ops-module.md`).
