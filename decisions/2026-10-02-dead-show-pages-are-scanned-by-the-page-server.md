# Dead show() pages are scanned by the page-server service, not the agent host

## Context

A busy agent gets no heartbeats, so the heartbeat-driven page probe never reaches it and
its dead pages stayed dead for as long as its turn lasted (the 2026-09-01 incident, about
four hours). The fix of the time was a periodic scan inside the agent-host process
(`reconcile_all_open_pages`), which re-ran the agent's own page pass for every hosted agent
under that agent's turn identity: probe every open page; re-serve a dead `serve()` page
through `ava.ui.serve`; close a dead `show()` page and tell the owner once.

## Decision

The periodic scan becomes a resident loop of the `page-server` service, which already owns
the table, the machine and the page sessions (`services/page_server/dead_pages.py`, beside
its reconcile loop under one `TaskGroup`, each with its own progress tracker). It scans only
the open show() pages of its host. A dead one is closed and its owner told in one
transaction, with the notice wording, the six-hour dedupe window and the statements shared
with the agent-side pass through `base/agents/page_recovery.py`; the loop then publishes the
`PageClosed` event and wakes the agent.

The re-serve arm is not carried over. A dead `serve()` page's server is relaunched by the
page-server daemon's own reconcile, in its persistent session, within one poll; the scan's
`ava.ui.serve` only registered the row again and waited for that same daemon, so it added
nothing and needed the agent kernel's identity binding, which a service may not import. The
agent's own boot and heartbeat passes keep their full arms.

No throttle column was added: the in-process per-agent stamp existed to coordinate the
periodic loops with the heartbeat pass, and with those loops gone nothing reads it. A
restart probes every open show page once, which is one cheap HTTP call each.

## Alternatives rejected

- **Keep the scan in the agent host.** It is not agent work: it needs no turn, only the
  table and a port probe, and it ran under a borrowed agent identity.
- **A `last_probed_at` column on `agent_pages`.** Persistent throttling of a pass that is
  already idempotent and cheap, with nothing left to coordinate with.

## Consequences

- If the page-server daemon is down, a dead show() page is no longer closed until it is
  back (the agent-host scan used to run regardless); the agent's heartbeat pass still
  covers an idle owner.
- The agent host loses one loop and its Redis event publisher; `_background_loops` no
  longer takes a pool.
