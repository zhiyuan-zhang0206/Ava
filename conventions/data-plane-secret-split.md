# Data-plane credential split

The cluster bearer and data-plane credentials have separate authority. The
internal data plane always authenticates, whatever the bearer
([decision](../decisions/2026-09-26-internal-data-plane-always-authenticated.md)).

| Authority | Holder | Purpose |
|---|---|---|
| `AVA_CLUSTER_SECRET` | Gateway only | Human/operator bearer for the gateway API and frontend login (never served by bootstrap, never held by a remote unit); empty = unauthenticated user-facing API and `/ops`, loopback-only listeners |
| `AVA_API_TOKEN` (launch environment; also `$AVA_HOME/run/ava-root/manifests.json`, 0600) | Each launched service, admitted operator processes | The write generation's machine API token of the process's class: the gateway admits the active generation's tokens, a unit's `/ops` its generation's two; delivered only while the API is authenticated. The manifest copy is root's own record of what it launched — it persists until the next start rewrites it, and is inert once a release fence revokes the generation |
| `$AVA_HOME/backups/logical-backup.passphrase` (0600) | Gateway home | The logical-backup passphrase: minted and pinned at birth (a home born earlier carries `sha256(secret)`, pinned once), never derived and never changed by a secret rotation ([decision](../decisions/2026-09-28-backup-passphrase-minted-at-birth.md)); **backup-critical**: it is the only key to every logical backup |
| OS user over the owner-only socket (`peer`) | Gateway host | Postgres administrator: provisioning, migrations (acting as the NOLOGIN schema owner), grants, the authority invariant |
| OS user mapped to `ava_monitor` (`peer map=ava_monitor`) | Gateway host's OTel collector | Password-less statistics reader (`pg_read_all_stats`, CONNECT); not a write generation, so no credential exists |
| `$AVA_HOME/db-authority/` (0700; files 0600) | Gateway home | `ledger.json` (owner, groups, active generation), `generations/0.json` (the write generation's two logins with passwords and SCRAM verifiers, and its two machine API tokens), `pooler-admin.json` (PgBouncer admin console `ava_pooler_admin`) |
| `$AVA_HOME/db-authority/` (0700; files 0600) | Remote agent-runner home | `unit.json` (the installed runner login of one generation, bound to this unit and the served endpoint, with its API admission: the runner API token, the gateway token's digest and the telemetry token) |
| `AVA_REDIS_ADMIN_PASSWORD` | Gateway only | Redis `default` user and `requirepass` |
| `AVA_REDIS_PASSWORD` | Gateway file; embedded in `AVA_REDIS_URL` | Redis ACL runtime user |
| `AVA_RUNNER_DB_PASSWORD` | Remote-managed planes only | The provider-provisioned `ava_runner` login the gateway-local launcher projects for agents (never served by bootstrap) |

A local plane's `.env` carries only the credential-free database endpoint
(`postgresql://<owner>@host:port/<db>`). The schema owner is `NOLOGIN` without
a password. Every application login is the home's one write generation: `ava_g0_gateway`
and `ava_g0_runner`, which inherit the `NOLOGIN` groups `ava_gateway` /
`ava_runner` and own nothing (`base/cluster/authority/`).

Delivery:

- The root launcher puts each DB-using service's class login into that
  service's launch environment (`AVA_DB_URL` plus the non-secret
  `AVA_DB_GENERATION`); gateway-profile services get the gateway login, runner
  and agent processes the runner login. The launch digest binds the generation
  number and credential digest, never a password. The same login also lands at
  rest, 0600, in `$AVA_HOME/run/ava-root/manifests.json` — root's own launched-unit
  record, rewritten on the next start.
- An operator process on the gateway home (the `ava` CLI, a script, an OS job)
  receives the gateway login only while it runs the home's admitted runtime:
  the source checkout the home was born from. Anything else keeps the
  credential-free endpoint and its first dial fails with
  `NoDatabaseAuthorityError`.
- Bootstrap serves no database credential: its `AVA_DB_URL` is the
  credential-free endpoint (a remote-managed provider URL loses its password),
  and a runner strips any password an older gateway still serves. A remote
  agent-runner's root launcher delivers its installed unit capability to every
  runner-class service, and an admitted operator process on that home consumes
  it; anything else refuses by name. The capability arrives only as an
  operator-issued bundle ([runbook](runbook.md)), so a stale runner cannot
  reacquire the current generation. Bootstrap never serves the human secret
  either, and the fetch authenticates with the unit's API token.

Agents never receive an admin password, `AVA_REDIS_PASSWORD` as a standalone
variable, or a gateway-class login. Agent-profile startup at the default home
rejects a loopback non-runner URL on a secret-bearing cluster.

## Homes born before this model

A home born before the data plane always authenticated has empty Redis
credentials, a LOGIN schema owner, a LOGIN `ava_runner`, trust `pg_hba` lines
and no database authority ledger. `ava start` refuses it before any native
effect, and `scripts/data_plane_ops/rotate_data_plane_secrets.py` refuses it too; nothing
converts it. Re-birth it as a new home.

Verify that a home carries no retired credential key without printing
credentials:

```bash
grep -E '^(AVA_DB_ADMIN_PASSWORD|AVA_RUNNER_DB_PASSWORD|AVA_REDIS_ADMIN_PASSWORD|AVA_REDIS_PASSWORD)=' "$AVA_HOME/.env" | cut -d= -f1
ava status
```

Only the two Redis key names may print; do not echo values, paste URLs into
tickets, or put passwords in command arguments.

## Routine data-plane rotation

PostgreSQL has nothing to rotate: the owner never logs in, and the application
logins are the home's one write generation, which nothing replaces.
`scripts/data_plane_ops/rotate_data_plane_secrets.py` rotates the Redis credentials only; they
do not rotate with the write generation
([decision](../decisions/2026-09-27-write-generation-rollout-choices.md)).

Run it on the gateway checkout that owns the target cluster, in a gateway
process context (not an agent shell). It defaults to dry-run and has no
`--home` flag.

```bash
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope runner --execute
```

`--scope admin` rotates the Redis `default` password (`requirepass`);
`--scope runner` rotates the Redis ACL runtime password; the default is both.
After runner scope, restart the gateway and every enrolled runner so each
reloads its Redis URL. Each execute writes a 0600 recovery file beneath
`$AVA_HOME/backups/secret-rotation/`; on failure, re-run the exact printed
`--execute --resume <state-file>` command rather than editing one side by hand.

## Emergency bearer rotation

`AVA_CLUSTER_SECRET` does not rotate with the data plane, and machine callers
never hold it. Rotate it only for a confirmed leak:

```bash
.venv/bin/python scripts/data_plane_ops/rotate_cluster_secret.py              # dry run
.venv/bin/python scripts/data_plane_ops/rotate_cluster_secret.py --execute
```

The script stages the next secret (`backups/secret-rotation/bearer.pending`),
journals the rotation as fingerprints (`backups/secret-rotation/bearer.json`),
verifies the pinned logical-backup passphrase (the secret never touches it;
a home without a pin pins `sha256(secret)` here), and only then writes the new
secret into the gateway `.env`; a re-run
resumes an interrupted rotation from its journal. Restart the gateway, then
issue every remote unit a new capability bundle: its telemetry relay token
derives from the secret. New browser logins use the new secret.
