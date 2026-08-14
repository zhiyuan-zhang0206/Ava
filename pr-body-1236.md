## What

Runner processes stop dialing Postgres with the full `ava_main` write credential (Task #1236). A new fixed least-privilege role `ava_runner` (LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE) carries exactly the audited runner surface; the bootstrap contract gains a role projection so runner fetches receive the `ava_runner` AVA_DB_URL with its own password.

The MyAva-class pollution (agents + agents_meta INSERT with the full prod write credential) becomes structurally impossible once runners dial this role: those writes — and any DDL — fail with a permission error at the DB, not at fixture discipline.

## File-tree diff

```
agent/
└── _process_boot.py            (M)  ★ agent boot skips checkpointer.setup() when the 4
                                     checkpoint tables exist (PG refuses CREATE TABLE IF
                                     NOT EXISTS without schema CREATE — the runner holds
                                     none by design); fresh-schema boot still creates them
cli/
├── install_cluster.py          (M)  ★ birth: mint/persist AVA_RUNNER_DB_PASSWORD (existing
│                                    .env value never rotated), thread through data-plane
│                                    bring-up, ensure_checkpoint_schema + ensure_runner_role
│                                    after provision
├── commands/
│   ├── _cluster_instance.py    (M)  runner_password threaded through ensure_cluster_instance
│   │                                → _start_pgbouncer (birth); resolved from the home .env
│   │                                on later bring-ups
│   ├── _pgbouncer.py           (M)  ★ userlist gains "ava_runner" "<pw>" entry once a runner
│   │                                credential exists; runner_password_from_env() reads the
│   │                                .env FILE (never env/settings — no ambient leak)
│   ├── cluster_lifecycle.py    (M)  _ensure_cluster_instance passes runner_password
│   ├── ensure_runner_role.py   (A)  ★ new `ava cluster ensure-runner-role`: the same
│   │                                idempotent SQL as birth (role + grants + checkpoint
│   │                                schema), mints/persists the credential, refreshes the
│   │                                pooler userlist when the pooler is running
│   └── __init__.py             (M)  export cmd_ensure_runner_role
├── parsers/cluster.py          (M)  ensure-runner-role subcommand
├── main.py                     (M)  handler import; ensure-runner-role joins the anchored-
│                                    home verbs (never reaches prod from an unanchored checkout)
└── enroll.py                   (M)  enroll fetch requests the runner projection
gateway/
└── routers/bootstrap.py        (M)  ★ GET /api/bootstrap?role=runner → projected
                                     AVA_DB_URL; unknown role / missing credential → 400
scripts/
└── rotate_cluster_secret.py    (M)  pooler rewrite keeps the ava_runner userlist entry
shared/
├── bootstrap.py                (M)  ★ fetch_bootstrap_config(role=...) appends ?role=runner;
│                                    inject_config_from_gateway always requests the runner
│                                    projection (every fetch this module makes is a runner)
├── cluster/
│   ├── derive.py               (M)  RUNNER_ROLE / RUNNER_DB_PASSWORD_ENV constants
│   ├── provision.py            (M)  ★ ensure_checkpoint_schema (checkpoint tables created
│   │                                AS the main role) + ensure_runner_role (idempotent role
│   │                                + the design's grant matrix)
│   └── __init__.py             (M)  re-exports
└── config/service_read.py      (M)  ★ bootstrap_config_values(role=): AVA_DB_URL userinfo
                                     swapped to ava_runner + its password (inside the URL,
                                     never a standalone key); fails loud without a credential
conventions/runbook.md          (M)  least-privilege runner role paragraph
frontend/
├── openapi.json                (M)  regenerated (role query param)
└── src/lib/types-generated.ts  (M)  regenerated
tests/
├── shared/test_runner_role.py  (A)  ★ contract tests on throwaway pg: the grant matrix
├── shared/test_bootstrap_runner_projection.py (A)  projection unit tests
├── shared/test_config_fetch_source.py (M)  stub gateway asserts ?role=runner on the fetch
├── cli/test_ensure_runner_role.py (A)  CLI end-to-end against throwaway pg
├── cli/test_pgbouncer_runner_entry.py (A)  userlist entry + env-file read
├── cli/test_install_cluster.py (M)  birth stubs accept runner_password; new steps stubbed
├── cli/test_enroll.py          (M)  fake fetch asserts role=runner
├── gateway/test_bootstrap_endpoint.py (M)  endpoint projection tests
└── scripts/test_rotate_cluster_secret.py (M)  rotation keeps the runner entry
```

## Data flow

**Fresh birth** (`install.sh` → `_birth`): mint `AVA_RUNNER_DB_PASSWORD` (existing .env value wins) → thread into the pooler userlist → `provision_database` (schema as main role) → `ensure_checkpoint_schema` (LangGraph tables as main role — the gateway's checkpoint readers dial that role) → `ensure_runner_role` (grants target existing tables) → persist the password in the gateway .env.

**Runner credential acquisition** (`ava enroll` / every runner process start): `GET /api/bootstrap?role=runner` — the gateway projects the served `AVA_DB_URL` onto `ava_runner` + its password (host/port/db/query survive; `_serve_reachable_data_plane_hosts` runs first, so the reachable-host rewrite still applies). No-secret clusters unchanged: the endpoint still serves without auth, and the projected URL works under loopback trust.

**Pooler**: userlist carries both entries (main role + ava_runner) when a runner credential exists; `ava start` and secret rotation rewrite it from the .env FILE.

**Agent boot as ava_runner**: skips `checkpointer.setup()` when the checkpoint schema is present — PG 17 refuses `CREATE TABLE IF NOT EXISTS` without schema CREATE (verified empirically; the contract test asserts the refusal), and setup() is a semantic no-op on an up-to-date schema. The gateway-side setup() calls (main role) remain the owner of LangGraph's own migrations.

**Design correction found by the e2e suite**: the design audit listed `inbound_messages` as SELECT/UPDATE-only, but `ava.self.terminate` / `restart` / `compact` insert their own inbounds directly from the runner process (not via the gateway API) — without the INSERT grant, a self-terminate never lands and the agent stays `running` forever (e2e caught it). The same audit pass (agent-side INSERT/UPDATE statements) added: `agents` UPDATE (`ava.self.set_label`), `agent_tasks` + `agent_watchers` INSERT/UPDATE (`ava.tasks` / `ava.watcher`), `agent_pages` UPDATE (page close at exit), and sequence USAGE for the BIGSERIAL INSERTs. The core protection is unchanged: agents INSERT, agents_meta INSERT, DDL still denied.

## NOT tested / deferred

- The real pooler path with two userlist entries end-to-end (scram auth as ava_runner through pgbouncer) — covered by unit tests of the rendered userlist + the existing integration bring-up; the staging-01 live enroll (#1818) exercises it.
- `pg_hba` tightening (delete the 100.64.0.0/10 broad range, ava_main loopback-only) is Task #1236-B — deliberately NOT in this PR; the runner role is usable under the current hba.
- Runner processes on prod are still dialing the main identity until the cutover rollout — this PR ships the code, the ops migration (#1818) switches them.
- Fresh-cluster agent boot as ava_runner was not exercised against a real born cluster (no cluster runtime touched per the task constraint) — covered by the contract test's setup() refusal + the boot skip logic. The e2e suite (real gateway-spawned agents on a throwaway cluster) runs in CI and exercises the runner role end to end; self-lifecycle paths (terminate / restart / hibernate / compact) verified locally.
- Column-level grants (agents_meta / agents cross-agent UPDATE residual risk) — accepted per design, deferred to v2.
- The `task_maintenance` daemon's `agent_notices` INSERT (ava_fleet plugin) is not granted to the runner role — it runs on the gateway; if a split runner ever hosts it, staging will surface it.
