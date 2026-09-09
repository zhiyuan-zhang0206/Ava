# Standing cron 7-day cap with renewal; terminated owners' watchers reaped

## Context

Task #2617 (TTL audit #2615, gap 3): watcher sessions deliberately carry no
shell TTL row, and the only recovery path is the agent's own boot reconcile —
an agent that terminates never reconciles again, so its standing cron rows +
processes leak forever (and each fire even auto-resurrects the corpse via the
chat-delivery path). User ruling 2026-09-09 16:53 (option 5a): a standing cron
defaults to a **7-day cap** — expiry requires renewal; longer schedules must
pass an explicit `end_time`.

## Decision

1. `ava.watcher.cron(expr, ..., end_time=None)` defaults `end_time` to now
   (minute-truncated) + 7 days; explicit ends are untouched.
2. Re-registering the same standing schedule **renews** it: EVERY live twin
   is superseded (new session + fresh end; each old session's deliberate kill
   drops its row) — never stacked into a double-firing duplicate. Superseding
   all twins (not just the newest) is what lets a repeated renewal converge
   after a partial failure left two live rows behind. The reconcile's dedupe
   is schedule-level (expression + timezone, any end time): a live
   same-schedule row subsumes a dead twin's rebuild.
3. The gateway TTL reaper reclaims watchers whose owner is terminated for
   good (`termination_source` user / exit / integrity / legacy NULL): kill via
   `shell_kill`, then mark the row `reaped` only while the owner is STILL
   terminated in the same statement.
4. Existing NULL-end rows converge via `TEMPLATE_VERSION` 4→5: the boot
   reconcile's stale-template machinery rebuilds live standing crons through
   the SDK, whose default now stamps the 7-day end.

## Alternatives rejected

- **One-time SQL backfill of NULL ends to now+7d** — stamps rows without
  reaching the running processes (their scripts bake the end at launch), so
  the processes would keep firing past their row's end until someone rebuilt
  them anyway. The template bump reaches both row and process in one
  mechanism.
- **Terminate-triggered cleanup hook** — would need its own retry story for
  unreachable machines (a failed kill on a dead machine orphans the live
  session); the periodic reaper pass reuses the existing shell-kill dispatch
  + retry discipline and sweeps the pre-existing backlog on its first pass.
- **Reaping crash corpses too (`reaper` / `launch-confirm` sources)** — they
  are auto-resurrect-eligible and their own cron wakes are a revival channel;
  touching them would race the crash-resurrect controller (the #2589 / #1938
  misfire lesson).
- **Renewal by killing the old session first, then re-registering** — a
  failed spawn between the two leaves the schedule dead; the supersede path
  registers the new row before the old session dies.
- **Exact-end dedupe only (no renewal semantics)** — the #1825 dedupe matches
  end times exactly, so every re-registration of a defaulted schedule would
  stack a new watcher; all of them double-fire until each expires.
- **Newest-only twin supersede** (QA review of PR #2037) — a partial failure
  (crash after commit before kill, kill with failed row delete) leaves two
  live different-end twins, and a newest-only supersede never reaches the
  older one; superseding every live twin + the reconcile's schedule-level
  liveness check are the converging pair.

## Consequences

- Agents that want a schedule longer than 7 days must re-register before
  expiry (the renewal call) or pass an explicit `end_time`; the ava-watcher
  skill documents this.
- A budget-exhausted crash corpse (auto-resurrect gave up) still leaks its
  standing crons — accepted as the price of never racing the resurrect path.
- A resurrected agent does NOT get its pre-termination crons back (the rows
  are `reaped` history, never rebuilt) — it re-registers what it needs.
