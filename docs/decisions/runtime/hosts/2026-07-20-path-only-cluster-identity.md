# Path-only cluster identity: the cluster name retires wholesale

**Decision (2026-07-20):** a cluster's identity is its home path. The cluster
*name* — as a settings field (`AVA_CLUSTER`), an identity file
(`$AVA_HOME/cluster`), a CLI input (`ava start --cluster`), a registry key, a
db/role/ACL-derivation input, a session segment, and a bootstrap
payload field — is deleted, not deprecated. Single-machine self-reference =
the `$AVA_HOME` path; cross-machine reference = gateway URL + cluster secret;
human display = the home's basename, computed on the fly. Birth moves entirely
into `scripts/install.sh` (`python -m cli.install_cluster`); `ava start`
becomes a pure bring-up that fails fast on an uninstalled home.

## Why the name had to go (not merely be tamed)

Every phantom-cluster incident in this repo's history was the *name* being
resolved from the wrong input: the cwd-basename fallback birthing a cluster named after the
user's home directory from a bare `ava start` in `~` (which displaced prod's
login Chrome), `AVA_CLUSTER` leaking through a shell into the wrong home,
`--cluster` pointing the prod `ava` at a dev cluster. Each incident grew a
guard (the one-home-one-cluster agreement check, the `main`/`source` bootstrap
refusal, converge's fails-open patch), and each guard added resolution rules
that themselves needed tests and carve-outs (the pure-agent-runner exemption).
The name was a *second* identity channel that had to be kept consistent with
the home — and every consistency mechanism is a place to drift.

Two premises had made the name look necessary, and both had already dissolved:

1. **"Shared namespaces (Postgres identifiers, redis ACLs, sessions)
   need a per-cluster token."** The per-cluster data plane removed the sharing:
   every cluster owns its own single-tenant pg/redis instance, so the db/role/
   ACL identifier needs no distinction (the fixed `ava` suffices), and the session
   socket was already per-home — the session name never needed a cluster
   segment once the socket dropped its own.
2. **"Identity must cross the network, and a path can't."** What actually
   crosses the network is a URL + the cluster secret; the `AVA_CLUSTER` field
   in the bootstrap payload only fed the runner's session prefix and display.

## Rejected alternatives

- **v1: birth-in-start hardened (cold-start + boot quarantine inside
  `ava start`).** Kept `ava start` as the birth path and added a quarantine so
  a fresh worktree's boot could not read the prod `.env`. Rejected because it
  preserved the double duty of `start` (birth + bring-up) and with it the whole
  name-resolution surface; install-as-birth makes the identity decision happen
  exactly once, in a process whose home is pinned explicitly.
- **Mid-state: home anchor + name as a "license plate" (both kept).** The home
  would be authoritative and the name a validated label stored in
  `$AVA_HOME/cluster`. Rejected because it keeps two channels that can
  disagree — the agreement guard IS the cost — and the label bought nothing a
  computed basename doesn't.

## What the cut changed (mechanism inventory)

Deleted: `cluster_name()` + cache, `validate_cluster_name`/`InvalidClusterName`,
`db_name`/`role_name` name-derivations, `gateway_home_for`/`cluster_from_home`,
the `$AVA_HOME/cluster` file, `ava start --cluster/--gateway-home`, the
`AVA_CLUSTER` settings field + `.env` key + session-env forwarding + bootstrap payload
field, birth-in-start (`maybe_birth_cluster`/`cmd_cluster_up`) with its
cwd-fallback and both collision guards, and converge's fails-open patch
(criterion now `is_default_home`).

Converted: registry keyed by home path (converge migrates the file
idempotently); management verbs address `--path`; db/role/ACL fixed to `ava`
for new births; launchd/cron/native labels use a home slug (basename + 8-hex
path hash); display label = basename; session socket `$AVA_HOME/run/sessions`, sessions
`ava-<service>`.

Kept: the unanchored DB sentinel (bare scripts in an uninstalled checkout still
cannot reach prod) and the `.ava_home` pointer (checkout→home anchoring is now
the identity resolution itself).

**Names-as-data is what decouples the code cut from the ops rename**: every
idempotent ensure path (role password re-affirm, redis ACL re-affirm, pgbouncer
identity, admin socket dial) reads the db/role/ACL identifier from the
cluster's own `.env` URLs (`shared.cluster.identity_from_url`) instead of
re-deriving it — so prod keeps running as `ava_main` after this lands, and the
`ALTER DATABASE/ROLE ... RENAME TO ava` + redis-ACL + `.env`-sed rename is a
separate, unhurried ops window.
