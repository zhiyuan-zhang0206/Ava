# The stop window: what holds, what releases, and where recovery resumes

## Context

The maintenance stop of 2026-09-12 (issues #2307, #2308; fix batch PR #2315,
task #3227) hung the data-plane phase and left a half-shut cluster: pgbouncer's
SIGTERM stop waited for paused runners' idle clients to disconnect, and when
the phase finally failed there was no compensating action — the pooler stayed
half-shut and the cluster dark. The companion defect: paused agent-runners kept
pooled client connections open to a data plane that was closing, so the stop
could not shed its clients even in principle. A crashed orchestration's lapsed
deploy lease also froze admission cluster-wide.

## Decision

1. **The pooler stop signal is SIGINT; Postgres takes `pg_ctl -m fast`; no
   force.** PgBouncer >= 1.23 runs SIGTERM as `SHUTDOWN WAIT_FOR_CLIENTS`
   (wait for every client to disconnect) and SIGINT as
   `SHUTDOWN WAIT_FOR_SERVERS` (disconnect clients, wait only for in-flight
   queries). Behind paused runners the first wait cannot complete — clients
   never leave on their own — while the held drain has already justified
   disconnecting them. `-m immediate` stays unreachable: an incomplete stop is
   reported (with per-phase accounting), never escalated into force.
2. **A failed data-plane stop compensates.** Only a failure inside the
   data-plane phase gets the bounded internal `ava start`
   (`--persist-services`; preserved sessions arrive as transient skips, and
   `ensure_pgbouncer`'s degraded-restart branch revives the half-shut pooler).
   A failure before the phase cannot present the half-shut shape, and after
   `_mark_stopped` the destructive work is done — both stay report-only. The
   outcome (`compensated` / report-only) lands in the stop report and journal.
3. **The quiesced window is `drained` .. `ready`.** From drain completion
   until `ava start` releases the hold, background database writers hold:
   ownership renewal, page reconciliation, close-notice flush. Agent-row
   leases lapse with their TTL; the first beat after resume refreshes every
   row this host still owns.
4. **The turn scan holds only through the stop leg (`drained` .. `stopped`),
   never the start leg.** From `starting` on, a booting host scans its pending
   workset and restores parked watcher intent even while the unit is still
   held: pub/sub has no replay, so a hint published while nobody subscribed is
   lost, and recovery may not wait for the hold to release. Execution stays
   fenced by admission — scanning early is free, missing the boot workset is
   not. (The first cut held the scan across the whole window and regressed
   `test_held_start_then_resume_restores_parked_watcher[False]`; that test is
   the spec for this slice.)
5. **Idle pool connections are released before the data plane closes.**
   `cluster_stop` releases every idle connection in the host's shared/control
   pools (loopback `POST /release-db-pools`) and the ops daemon's dispatch
   pool; all pools run `min_size=0`, so the first borrow after resume
   reconnects lazily. `drain()` and `close()` cannot serve this window
   (`drain()` re-opens replacements, `close()` is terminal), hence
   `shared/pool_release` against the psycopg-pool private face.
6. **Admission defers on a non-stable deploy phase only while the deploy lease
   is live.** A crashed orchestration cannot come back to clear its lease; the
   durable `pending` rows still wait.

## Alternatives rejected

- **Longer deadline / SIGKILL escalation for the pooler.** A longer deadline
  postpones the same wait; force-killing skips the disconnect bookkeeping the
  stop exists to preserve. The incident needed the stop to COMPLETE under
  paused runners, not to wait harder.
- **Leave the half-shut pooler for the next start to heal.** That is the
  #2307 outcome — a dark cluster until an operator notices; the start path is
  not a repair tool.
- **Hold every loop across the whole window, scan included.** Holding the
  boot workset scan through the start leg loses recovery for hints published
  while nobody subscribed; the regression test above is the concrete cost.
  The scan is recovery machinery, not background chatter.
- **Keep pools open through the stop and rely on process exit.** Paused
  runners are precisely the processes that do not exit; their connections are
  what held the pooler's wait open. The stop must shed them explicitly.
- **Rollout tail writes through the pooled path.** `finish_update` /
  `release`/`settle_update_lock` must land when this rollout's own data-plane
  stop has just taken the pooled path down; they dial the direct URL.

## Consequences

- One more distinction to keep straight: `quiesced()` (no database work at
  all) vs `in_stop_leg()` (agent work not touched). Both refuse new work when
  the owner is unreadable.
- The pool release is best-effort and reported in the stop payload; a failed
  release does not fail the stop.
- Recovery correctness depends on the scan resuming at the start leg; a
  future hold added to the scan must carry this record's constraint.
