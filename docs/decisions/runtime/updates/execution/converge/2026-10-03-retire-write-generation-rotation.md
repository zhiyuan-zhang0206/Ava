# The database write generation has one generation and no rotation

## Context

The [always-authenticated decision](../../../../data/security/2026-09-26-internal-data-plane-always-authenticated.md)
made write authority rotate per rollout: a revoked generation's logins lose `LOGIN`, a census proves
its sessions closed, and the next generation is minted. That existed to keep a stale writer from
writing after a release changed. The
[release-path removal](../../release/2026-09-30-remove-release-image-path.md) deleted per-rollout rotation
and kept the machinery as a runbook procedure for a leaked credential; stale writers are
now kept out by the [code-version gate](2026-09-30-client-side-code-version-gate.md).

Two facts remove the procedure's last use. The user ruled that a secret that is not on the public
internet is not a leak, and an internal exposure (a log line, a Loki entry, a bundle on a trusted
machine) is not rotated. And the production ledger holds generation 0 only: no pending, no
revoked entry (read on the gateway host, 2026-10-03). With nothing left to prevent, the rotation
code was a second generation's worth of state machine, closure proof and crash-injection tests
that no operator runs ([postmortem 0009](../../../../../postmortems/0009-complexity-must-name-the-failure-it-prevents.md):
each piece of code names the failure it prevents).

## Decision

A home has exactly one write generation, born by its first start; nothing revokes or replaces it.

Kept, each for the failure it prevents:

- **Always-authenticated access**: SCRAM for Postgres and PgBouncer, the `NOLOGIN` owner and groups,
  generation 0 minted at birth, and the fail-closed catalog invariant at every start. Without them an
  application process holds owner or admin credentials, or a stray login or grant goes unnoticed.
- **Pending then active, secret published first**: an interrupted birth resumes with the credentials
  it already published, and nothing is accepted or delivered before the pooler serves the pair and both
  logins answer.
- **Ledger custody and the credential digest**: a corrupt, loosened or foreign store, or a secret that
  does not match the ledger, refuses instead of delivering.
- **Runner bundles, API tokens and the version gate**: a runner needs a login without the human secret,
  machine callers present a class token, and old code cannot write.
- **`retire_legacy_logins` at birth**: the owner is created able to log in for provisioning and is
  demoted to `NOLOGIN` once.

Deleted: `OperationAuthority`, `BirthAuthority` (a single-valued token), `begin_revoke`,
`mark_closed`, `record_drops`, `revoke`, `close_revoked`, `prove_closure` with its census,
`fenced_roles`, `sweep`, `prune`, the `counter` and `revoked` ledger fields with the `Revoked` and
`ClosureEvidence` records, the start-time sweep, the runner bundle's "not older than the installed
generation" check, the runbook's manual-rotation section and their tests.

Three things stay shaped like the removed design because production's files carry them. The role names
`ava_g0_*`, `generations/0.json` and the bundle's generation number stay, since renaming them is
itself a credential change. A ledger written before this decision, with `counter: 0`, `revoked: []` and
null `operation`/`direction` in its origin, still loads and is rewritten without them; a ledger that
records a rotation refuses as corrupt. The origin `cutover` stays readable.

## Alternatives rejected

- **Keep the machinery as a dormant tool.** It is the dead-code-with-tests shape that postmortem 0009
  names, it costs every authority change a second generation to reason about, and a procedure nobody
  runs rots.
- **Keep the start-time sweep as a self-heal.** With one generation it only acts on a foreign group
  member or a login-capable owner, which is an anomaly. The invariant refuses on those instead, so the
  operator sees the cause; a silent demotion hid it.
- **Keep the closure census for a restored catalog that resurrects an old login.** A restore is
  operator-run and the invariant already reports the stray member at the next start.
- **Convert the ledger to a new version.** No conversion exists by policy, and production's ledger has
  to load unchanged on the next update.
- **Drop `pending`.** An active record written before the roles exist turns an interrupted birth into a
  permanent invariant failure; `pending` is the crash-safe step.

## Consequences

- No command or procedure replaces the write generation. A credential that did reach the public
  internet is handled by re-birthing the home (`ava cluster destroy`, `ava init`), not by a rotation
  here. The human secret (`rotate_cluster_secret.py`) and the Redis passwords
  (`rotate_data_plane_secrets.py`) still rotate on their own.
- A start no longer repairs a catalog that drifted: it refuses and names the role.
- `ava cluster db-authority install-unit` stays for a fresher bundle (an expiry, a rotated telemetry
  token); a bundle must carry the installed generation's credential digest.
- Gateway session binding to the active machine token is unchanged; it simply has nothing that revokes
  the token while a gateway runs.
