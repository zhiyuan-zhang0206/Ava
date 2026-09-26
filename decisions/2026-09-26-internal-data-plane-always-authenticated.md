# Internal data plane always authenticates; write authority rotates per rollout

## Context

The cluster release transition must keep a stale writer from writing after a
release changes. A runner offline during an update can later rejoin with the
previous application image and its previous database credentials. A schedule
or terminal process that survived the application root can do the same on the
local host. Its code may be exactly the defective version being replaced, so
the fence cannot depend on the old code noticing that it is stale.

Two existing rules left no database-level fence. Data-plane credentials never
rotate after install. With an empty `AVA_CLUSTER_SECRET` (the single-box
default), every credential is empty and PostgreSQL/PgBouncer accept
unauthenticated loopback connections, so there is no credential to revoke.

## Decision

User ruling, 2026-09-26. PostgreSQL and PgBouncer always authenticate every
application connection with generated credentials, including when the
control-plane bearer is empty. The frontend and HTTP API keep their existing
bearer rule: an empty secret still means no user-facing login.

Write authority is a per-rollout generation, separate from the schema/protocol
version. Every upgrade and every rollback creates a fresh pair of restricted
application logins (gateway, runner) that inherit stable NOLOGIN capability
groups and own no objects. Returning to a previous image uses a new
generation, never the revoked one. The transition revokes the previous
generation, closes the owned pooler, and positively verifies that old sessions
and prepared transactions have ended before it admits the replacement.

The design, remaining work and verification requirements live in
[unified cluster lifecycle](../future/infra/unified-cluster-lifecycle.md),
section "Proposed database authority boundary".

## Alternatives rejected

- **Keep the internal data plane unauthenticated when the secret is empty.**
  The stale-writer fence would depend on old application code checking its own
  version and exiting. That trusts the defective code the release replaces,
  and it leaves surviving local schedule/terminal writers unfenced.
- **Keep one secret switch for both layers; fence only when the secret is set.**
  Offline remote runners exist only in multi-machine clusters, and a single
  box could rely on native custody of its local processes instead. Rejected:
  it keeps two data-plane postures and two fencing modes, and development and
  preview clusters (single box, empty secret) would never exercise the fence
  that production depends on unless configured specially. The user-visible
  switch is unaffected either way; only internal credentials differ.
- **Rotate only on incompatible schema changes (compatibility epoch).** An
  ordinary same-schema patch would leave an offline old runner able to write,
  and the user requires that runner to stay fenced after ordinary patches too.
- **Per-agent or per-process database roles.** Finer than the threat requires;
  the rollout generation already separates old and new writers.

## Consequences

- The empty-secret rule in `AGENTS.md` ("keeps every credential empty") changes
  when this is implemented; until then it still describes current behavior.
- Every rollout must deliver fresh credentials to each participating unit
  through the verified launch of its captured image, bound into the launch
  proof, with crash-safe retry that reconciles the exact existing generation
  instead of minting another. Offline units receive current authority only
  after they converge.
- The bootstrap endpoint that exchanges a cluster bearer for fresh database
  credentials must be retired, or a stale runner could re-authorize itself.
- The database fence does not revoke a stale caller's HTTP bearer. API
  admission needs its own generation boundary before stale-writer exclusion is
  complete.
- Operators debugging with `psql` read the generated credentials from the
  home configuration instead of connecting without a password.
