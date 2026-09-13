# Isolated physical restore drill (PITR)

`ava pitr drill` restores one protected physical backup chain to an
operator-chosen target LSN in an isolated sandbox. It is the first-class
version of the 2026-09-13 WSL D4 drill: the same source modules
(`restore_drill` composes `restore_proof` download helpers,
`base_restore_crypto`, `restore_manifest` and `restore_postgres`), the same
acceptance criteria, no ad-hoc driver.

## When to run it

- After an activation reaches `protected`, to prove the chain restores to a
  chosen point in time (the activation restore proof only proves the recorded
  end LSN).
- On the operator's own schedule, or before a planned intervention.
- Never overwriting anything: the command refuses a scratch tree that already
  exists, publishes nothing, and never writes to the live cluster.

## Prerequisites

- Run it on the host that owns the chain's local ACK evidence
  (`$AVA_HOME/physical-backup/ack`): the WAL allowlist is built from the ACKs
  and the sandbox is local.
- The PITR store settings and viewer-only credentials must be configured for
  the selected backend (`AVA_PITR_STORE_BACKEND` plus that backend's viewer
  file); secrets live under `$AVA_HOME/secrets/` (e.g.
  `oss-pitr-viewer.json`, `pg-backup.key`).
- Disk: at least twice the base ciphertext size (quarantine copy + extracted
  pgdata) plus the WAL chain. The 2026-09-13 drill used ~5.4 GiB for a 4.0 GiB
  base.
- Egress where the store needs it (WSL to OSS requires
  `HTTPS_PROXY=http://127.0.0.1:7897`); the command inherits the invoking
  shell's environment, so export the proxy variables there.
- The command must be its process-group leader: run it under `setsid`, so the
  sandbox postmaster shares the group and a crashed drill stays reapable.

## Pick the target

Sample a target from the live cluster a few minutes before starting
(read-only):

```sql
SELECT pg_current_wal_lsn(), clock_timestamp();
```

Record both values. The LSN is the primary target; the wall clock is required
for the double-face criterion. Write the wall clock with an explicit offset
(`2026-09-13 13:13:03+08`); a bare abbreviation such as `CST` is rejected
because PostgreSQL reads it as -06:00.

The target must lie at or after the chain start. It may lie *beyond* the
candidate manifest's recorded end LSN (a chain keeps archiving after its
candidate was captured) -- the ACK evidence is the upper bound, and a missing
segment fails the drill before anything starts.

## Run it

```bash
export HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897 \
       http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897 \
       NO_PROXY=127.0.0.1,localhost,::1
cd "$AVA_HOME/source" && setsid nohup .venv/bin/ava pitr drill \
  --chain activation-<...> --target-lsn 26/A03520B0 \
  --target-wall '2026-09-13 13:13:03+08' \
  --scratch "$HOME/pitr-drill-$(date -u +%Y%m%dT%H%M%SZ)" > drill.log 2>&1 &
```

Progress goes to stderr and the final summary JSON to stdout. `--candidate
<path>` overrides chain resolution from
`$AVA_HOME/physical-backup/base-manifests/<chain>.candidate.json`.
`--promotion-timeout` bounds both postmaster start and replay to the target.

## Acceptance criteria (all must hold)

1. **Identity** -- restored `system_identifier` equals the candidate's and the
   major version matches. The post-promote `timeline` reads candidate timeline
   + 1: that is PostgreSQL promote semantics, not a mismatch.
2. **Arrival** -- the sandbox promoted (`pg_is_in_recovery() = false`) with
   `pg_last_wal_replay_lsn() >= target`; the log's stop lines are recorded
   (`recovery stopping ...`, `last completed transaction`).
3. **Double face** -- at least one `inbound_messages` row in the hour before
   the target wall clock, and nothing after the target plus the 5-minute
   timestamp tolerance.
4. **Availability** -- the four evidence tables count successfully
   (`inbound_messages` / `agent_tasks` / `agents` / `checkpoints`) and a known
   business query runs; restored and live counts are both recorded.
5. **Isolation** -- the live instance's identity is unchanged across the run,
   the sandbox is stopped, and the residue scan finds no process under the
   scratch tree, no listener on the sandbox port, and no pid file.

## Failure handling

- The scratch tree is kept on every outcome; `drill-evidence.json` (0600)
  carries the per-step record, the criteria and the failure reasons. A re-run
  needs a fresh scratch directory -- the evidence is never overwritten.
- Identity mismatch, a missing segment or a base-authentication failure are
  chain-integrity findings: report them before any cleanup and keep the tree.
- A surviving sandbox postmaster is reported by the residue scan. Stop it
  manually before re-running (`<pg_ctl> -D <scratch>/sandbox/data -m fast
  stop`), then verify no listener remains on the recorded port.
- A non-zero exit means at least one acceptance criterion failed; the
  evidence file says which.

## What it does not prove

- It does not replace the scheduled activation restore proof
  (`restore_proof.prove_candidate`), which restores to the recorded end LSN
  and carries publication authority.
- It publishes nothing and leaves no owner evidence outside the scratch tree:
  the operator owns the tree and its disposal.
