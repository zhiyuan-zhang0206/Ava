# Every resident database loop gates on `quiesced()`; the shared round loop does it once

## Context

`decisions/2026-09-12-stop-window-contract.md` made the span between a completed drain and
the release of the hold database-quiet, because a paused runner's idle pooled clients are
what held PgBouncer's stop open. Its third point named three writers (ownership renewal,
page reconciliation, the close-notice flush). The audit of resident loops found the rest
still borrowing the pool every tick through the window: the delivery watchdog, heartbeat,
labeler, events-maintenance, page-server, the IM bridge's notice poll, the TTL reaper, the
schedule manager's request consumer, the memory indexer on its pgvector backend, the
hierarchy tick, and the gateway's flushers. Each loop that did gate carried its own
hand-written check, and nothing made the next loop remember it.

## Decision

1. The shared round loop (`base/daemon/round_loop.py:run_rounds`) skips a round while
   `admission.quiesced()`. Every loop built on it — the TTL reaper's two, the schedule
   manager's two, the watchdog's three recovery loops, and whatever lands on it next — is
   gated without a line of its own. A skipped round beats progress as a sleeping loop does and
   never marks success.
2. A hand-written loop checks `admission.quiesced()` in its own function, after its wait: a
   held unit skips the pass, keeps beating liveness, and resumes when `ava start` releases the
   hold.
3. `scripts/content_lint/lint_quiesced_loops.py` makes the stance mandatory. A resident
   loop in `services/`, `gateway/` or `ops/` (`while True` or `while not <event>.is_set()`
   with a periodic wait) must gate in its enclosing function or carry
   `# quiesce-exempt: <reason>` on or above the `while` line. A marker no loop starts under is
   stale and fails. The check is lexical; it does not follow calls and does not see timer
   callbacks.
4. Deliberately not gated, each carrying its reason in a marker:
   - the host dispatcher's turn scan reads `in_stop_leg()`, not the whole window (contract
     point 4);
   - the pg-backup scheduler dials the direct URL read-only through `pg_dump`, never the pool,
     and a long pause must not suspend backups;
   - the IM adapters' pollers write a cursor only when an external update arrives, and
     forwarding goes through the gateway, which refuses business requests in the window;
   - the pause and stop commands' own bounded waits, and every loop that never touches the
     database.

## Alternatives rejected

- **Refuse the borrow at the pool while quiesced.** The pool also serves a finishing turn, an
  API call and the stop's own writes; making them fail is a different, wider change than
  holding the background writers.
- **A frozen baseline in the ambient-state rule.** It is shrink-only, so a new loop that
  needs no gate could never be added to it; the answer would be to gate loops that must
  keep running, such as liveness beats.
- **Rely on stop order.** Two orders exist (the CLI's per-unit `down`, alphabetical; root's
  reverse registration order), and a pause keeps its units alive through the whole window.
- **Gate every loop, database or not.** Health, supervision and file-rotation loops must keep
  running through the window.

## Consequences

- `round_loop` depends on `base.deploy.maintenance.admission`; a test that runs a round loop
  with a hold up sees rounds skipped.
- The check cannot tell whether a gate guards the actual borrow, so each gated loop has a
  test that a quiesced unit leaves the pool untouched.
- Event-driven writers (audit-event writers such as `record_audit_standalone`, the IM cursor stores) still touch the
  pool when an event arrives inside the window; they are not loops and are not covered.

Forward link (2026-10-03): `ava pause` was deleted; a stop with a different keep set replaces it. See [delete ava pause](2026-10-03-delete-ava-pause.md).
