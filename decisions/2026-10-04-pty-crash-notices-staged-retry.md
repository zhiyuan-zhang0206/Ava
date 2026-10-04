# A crash-notice batch is staged on disk and re-sent, not lost to the child's time limit

## Context

The pty-sessions service tells the owners of a crashed service's busy sessions
through a one-shot child with a thirty-second limit
([2026-10-04-pty-crash-notices](2026-10-04-pty-crash-notices.md)). On 2026-10-04
a live incident showed the limit's failure mode: a machine-wide crash left 83
busy sessions to sweep, the child wrote notices sequentially at roughly one
connection and one transaction per notice (~0.78 s each), the parent killed it
at the limit, and only 37 of the 83 were written; the tail was dropped with a
single WARNING and nothing retried. A 34-session batch wrote fully, so the
truncation threshold sat near 38 sessions.

## Decision

1. `write_notices` writes its whole batch in ONE transaction over one
   connection: every `api_idempotency` claim and every inbound insert commits
   together or not at all. The claims go in as one multi-row statement (its
   `RETURNING` names the keys this attempt owns) and the owners' statuses are
   read in one, so a batch of the incident's size costs a handful of round
   trips and the thirty-second limit stops binding long before the batch does.
2. The service stages the sweep's notices on disk
   (`$AVA_HOME/run/pty-close-notices.json`, `close_notices_path`) before the
   child starts, merging them beside anything an earlier start staged; the
   child removes the file only once every notice of it is written. A child cut
   short by the limit — or failing on a database that answers nothing — loses
   nothing: the service reports the leftover batch at ERROR with its count, and
   the next start re-sends it. The re-send reuses the same idempotency keys, so
   a notice that already committed is skipped, never delivered twice.
3. The stop path keeps its posture: `ava stop` still writes its notices
   directly while its database is up, and a stop-time failure stays loud on
   stderr without a retry
   ([2026-10-02-close-notices-written-at-terminals](2026-10-02-close-notices-written-at-terminals.md)) —
   the stop can write while the database is up, which is what that decision
   restated.

## Alternatives rejected

- **Keep per-notice transactions and widen the limit.** The limit is a
  liveness bound for a side channel running beside the serving loop, not a
  throughput knob; widening it only moves the truncation to a bigger batch.
- **Write the notices from the service itself.** The service stays
  database-free so a wedged pool or a slow database cannot stall the loop that
  answers every agent's shell; the child confines both.
- **A retry timer while the service runs.** The service holds no database
  client and spawns nothing on a schedule; the next start is the retry point
  the sweep's own lifecycle already provides. A service that never restarts
  leaves the notices staged — delayed, not lost.
- **Fail the service start when the notices cannot be written.** A notice is a
  side channel; the serving loop is not allowed to depend on it.

## Consequences

- A batch the child cannot finish is delivered by the next start instead of
  never. The notice can arrive arbitrarily later than the crash — the same
  delayed-backfill shape owners have already seen from stop-time notices.
- The staged file is new per-home runtime state under `run/`: written by the
  service, drained and removed by the child, re-written by the next stage. An
  unreadable file is logged (WARNING) and read as empty — never guessed at,
  and left in place for the operator, like the ledger.
- The child's input contract is the staged file, not stdin; nothing else calls
  the child.
