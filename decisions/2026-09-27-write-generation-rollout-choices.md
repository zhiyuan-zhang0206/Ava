# Write-generation rollout choices: explicit cutover, stable Redis, per-generation API tokens

## Context

[The always-authenticated data plane decision](2026-09-26-internal-data-plane-always-authenticated.md)
requires authenticated PostgreSQL, PgBouncer and Redis and a fresh database
login pair per rollout. Implementing it left four choices open: how existing
homes convert, whether Redis rotates with each generation, how the HTTP API is
fenced (the database fence does not stop a stale caller's HTTP bearer), and
whether networked rollouts may proceed before credential delivery is automated.

## Decision

User ruling, 2026-09-27, accepting each recommendation of the implementation
plan:

1. Existing homes convert through an explicit, idempotent one-time cutover
   script (`scripts/cutover_db_authority.py`), never implicitly on a cold
   `ava start`. Development and preview homes are recreated instead.
2. Redis always authenticates but its credentials do not rotate per rollout.
3. The HTTP API is fenced with per-generation machine tokens. Runners stop
   holding the human cluster secret, which rotates once at the fleet cutover. A
   single box with an empty secret keeps its open user-facing API.
4. Networked rollouts keep refusing until automated per-unit credential
   exchange exists. Manually carried per-unit credential bundles are only for
   the one-time production cutover and emergencies.

## Alternatives rejected

- **Convert on cold start.** Hides a security-posture change inside an
  ordinary start and makes partial conversions hard to observe and resume.
- **Rotate Redis every rollout.** Redis holds no durable business data, so the
  stale-writer risk it would close is small, while every rollout would gain
  another credential delivery and restart step.
- **Keep the shared human secret on runners and rely on the database fence.**
  A stale runner could still act through the API.
- **Allow manual credential bundles for routine networked rollouts.** Routine
  manual delivery is the error-prone path the generation model exists to
  remove.

## Consequences

- `ava start` refuses a password-less home and names the cutover script.
- Rotation of the Redis credential remains an explicit operator action.
- Fleet releases stay blocked on the automated credential exchange, which in
  turn depends on the fleet release transition.
