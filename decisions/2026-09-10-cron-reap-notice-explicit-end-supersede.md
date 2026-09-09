# Cron reap notifies the owner; explicit-end re-registration supersedes the standing twin

## Context

PR #2037 review findings (2026-09-09 batch): (1) a cron row reaped by the
terminated-owner reclaimer is `reaped` terminal history — the boot reconcile
never rebuilds it, so a resident agent's standing schedule silently
disappears after any resurrection; (2) the Task #1825 dedupe matches end
times exactly, so re-registering a schedule with an explicit `end_time` does
NOT supersede a standing twin — the documented "longer schedule" flow
(`cron(expr)` → `cron(expr, end_time=+30d)`) leaves two watchers of one
schedule live and double-firing until the older end passes. Issues #2060 /
#2061; user rulings 2026-09-10 01:52.

## Decision

1. **Reaped stays terminal, but the reap notifies.** When the terminated-
   owner reclaimer terminalizes a watcher row, it queues a reclamation
   notice for the owner in the SAME transaction as the mark
   (`UPDATE ... RETURNING name` + system inbound): "Watcher schedule <name>
   was reclaimed after its TTL expired. Re-register it with
   ava.watcher.cron() if it is still needed." — same caliber as every other
   TTL reclamation notice. The owner is terminated at mark time, so the
   notice is inserted WITHOUT the live-owner gate: it stays a pending
   inbound and delivers on the agent's next resurrect through any channel,
   never resurrecting the agent itself.
2. **One live watcher per schedule, any end times.** The twin supersede
   runs first for EVERY cron registration (defaulted renewal or explicit
   end): a live same-schedule row with a different end is superseded
   (new session, fresh end; each old session's deliberate kill drops its
   row). Only when no twin exists does the exact-end dedupe run — an
   identical live schedule is REUSED. The defaulted-end minute truncation
   stays, so a same-minute double registration still groups into one
   exact-match reuse.

## Alternatives rejected

- **Auto-restore a reaped schedule on resurrect** — the reaper cannot know
  whether the agent still wants it; a reaped row is a record of a decision,
  not a draft. The notice gives the agent the knowledge to re-register
  deliberately.
- **Live-only notice (`_notify_owner`)** — the owner is terminated by
  definition at reap time, so a live-gated notice would never deliver.
- **Explicit-end stacks, defaulted-end renews (status quo)** — the
  documented longer-schedule flow double-fires for up to 7 days; two
  explicit ends of one schedule double-fire the same way. One schedule,
  one live watcher is the simpler invariant.
- **Newest-only twin supersede** — same convergence hole as #2037's QA
  review found for renewal: a partial failure leaving two live twins is
  only collapsed by superseding every twin.

## Consequences

- A resurrected agent sees the reaper's notice among its pending inbounds
  and re-registers only the schedules it still needs — the silent-loss gap
  is closed without resurrecting the schedule behind the agent's back.
- Registration of the same (agent, expression, timezone) can no longer
  produce two live watchers from any call sequence — the reconcile's
  schedule-level rebuild dedupe is the mirror image, so a double-fire can
  be born in neither direction.
