# A stale writer is stopped by a client-side code-version gate

## Context

Production is one Linux gateway and four macOS runners, updated by a script
([source-mode updates](2026-09-30-networked-cluster-stays-on-source-updates.md)):
stop every unit, check out one exact commit on each, sync, start the gateway,
then the runners. A runner that is offline during an update (a closed laptop)
is not stopped and its checkout does not move. When it wakes, the processes it
was already running keep writing to the shared database with the old code.

The failure to prevent is exactly that: **old code writing after an update**. Two
existing checks miss it. The startup schema check (`CodeBehindSchema`) runs only
when a process starts, so it never sees a process that is already running, and
it fires only when the update carried a migration, so a code-only update passes
it. Nothing at all judges a process that never restarts.

The scale is one operator and five machines. A stale writer is a single machine
that comes back, and the operator can find and update it by hand once the
process stops writing.

[The 2026-09-26 decision](2026-09-26-internal-data-plane-always-authenticated.md)
answered the same failure with a database-level fence: a fresh pair of logins
per rollout, the previous pair revoked and proven closed. It rejected a
client-side version check because the fence "would depend on old application
code checking its own version and exiting". That release path was built, and
[postmortem 0009](../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)
records why production cannot use it: it was sized to a fleet-release system this
cluster does not need, and it costs more than the failure it prevents. Updates
stay on the scripted source path, and the source-mode decision left the stale
process to a version gate. This decision is that gate: it replaces the
rejection with the simple mechanism, and names what it accepts in exchange.

## Decision

`deployment_state.min_code_version` (`BIGINT NOT NULL DEFAULT 0`) is the lowest
code version allowed to write.

- **The version** is the number of first-parent commits reachable from the commit
  a process loaded (`git rev-list --count --first-parent <sha>`), computed once per
  process from `loaded_commit`'s boot capture, never from the checkout as it is
  later. On `main` every squash merge adds one, so a larger number is newer code.
  A tree that is not a git checkout has no version and fails fast: no fallback
  to `0`.
- **Raised on every update.** Each gateway start, after migrations and the
  schema assertion, sets the minimum to `GREATEST(min_code_version, <its own
  version>)`. Every update therefore cuts off every process still running the
  previous code, with or without a schema change. A rollback never lowers it.
- **Checked at every pooled session.** The baseline-session restore that every
  pooled dial and borrow already runs reads the minimum, folded into its
  second statement so it costs no round trip, at most once per 30 seconds per
  process. A process whose version is below it logs `critical` (its version, the
  minimum, its name) and calls `os._exit(78)`. It does not raise: `psycopg_pool`
  swallows an exception from a `check` or `configure` callback and retries until
  `PoolTimeout`, and `configure` runs on a worker thread nothing reads. `os._exit`
  acts from any thread and gives the old code no graceful shutdown to keep
  writing in.
- **Not gated:** direct connections (administrator, migration applier,
  `pg_dump`) and `connect_url` targets. The `ava` CLI is exempt by declaration,
  because `ava stop` writes to the database to drain agents: a host left behind
  must still be able to run the command that stops it, or the update script
  cannot recover it. Service processes start with `python -m <module>`, never
  through the CLI entry point, so all of them are gated.
- **Observable:** pooled connections carry `application_name = ava:<process>:v<version>`,
  so PgBouncer's `SHOW CLIENTS` lists which process on which version holds each
  connection.
- **Credential leak:** a runbook of manual rotation, no code
  ([runbook](../conventions/runbook.md#code-version-gate)). The on-disk
  authority format is unchanged.
- **Rollback:** lowering `min_code_version` is a manual step, in the same runbook
  section.

## Alternatives rejected

- **Per-rollout write generations and a fence (2026-09-26).** It stops a stale
  writer without trusting its code. It also needs per-unit capability bundles, a
  release journal, a finite executor and a fleet coordinator, none of which
  production can run today, and it cost days of blocked development
  (postmortem 0009). Kept only as the shape of a manual rotation after a leak.
- **The startup schema check alone.** Runs only at process start and only for
  updates that carry a migration.
- **Ask each machine at update time.** The machine that misses the update is the
  one that cannot be asked.
- **`host_version` (a `YYYY.M.D` date).** Does not order two commits of the same
  day.
- **Raise the minimum only on incompatible releases.** A code-only update would
  leave an offline runner writing with old code, which is the failure being
  prevented.
- **A database-side check.** The database cannot know a client's code; any value
  it compares is one the client reports, so it relies on the client just the
  same and adds a login hook the pooler cannot carry.
- **A separate polling thread.** A second mechanism beside the one every pooled
  session already passes through.

## Consequences

- **The gate trusts the process it stops.** A stale process that ignores or
  lacks the check is not stopped. This is not a security boundary against a
  hostile client; it is a guard against a forgotten one.
- **The first gated release cannot stop earlier processes.** A process running
  code from before the gate has no check to run, and no update delivers a
  safeguard to code that is already running
  ([a rollout cannot deliver its own protection](../postmortems/0001-a-rollout-cannot-deliver-its-own-protection.md)).
  The rollout that ships the gate is handled as every earlier one was, by
  stopping every unit; the gate protects from the next update on.
- **One long-lived connection is outside it.** The check runs when a session is
  dialed or borrowed; a process holding a single raw connection forever and never
  re-dialing is never re-checked. Every pool borrow is covered.
- **Every unit needs a full git clone.** A shallow clone counts only the commits
  it holds, so it looks older than it is and its processes exit 78. A wheel or
  retained-image runtime has no git history and cannot start. Production is
  full-clone source mode.
- **The minimum never falls by itself.** After a rollback the operator lowers it
  by hand; until then the rolled-back gateway exits 78 at its first dial, which
  is loud, not silent.
- **A supervisor revives a stale service into another exit.** Each attempt logs
  the critical line and exits; the cadence is the supervisor's, and the answer is
  updating the checkout.
- **A stale runner is refused from the next borrow on**, within 30 seconds of
  the minimum rising, not at the instant it rises.
