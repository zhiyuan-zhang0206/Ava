---
type: doc
title: Run Timeline Links
description: The agent-to-agent events (messages, spawns, forks, terminations, restarts, resurrections) with an end among given agents, for the arrows of the agent view.
tags:
- services
---

# Run Timeline Links

`GET /api/insights/run-timeline/links?agents=405,6657&from=&to=` (`run_timeline/links.py`; the gateway declares it in `gateway/routers/insights.py`) returns the audit events of the kinds `send_message`, `spawn`, `fork`, `terminate`, `restart`, `resurrect` in the window where at least one end is among `agents`, oldest first, paged like the lifecycle markers. `from` and `to` are required and carry a timezone.

- **Direction.** The sender is the audit row's `source` (`agent:N`, or `user` / `ui:page:*` for the user, sent as `sender: null`); the receiver is its `agent_id`. `target_agent_id` is not used: for a `send_message` it repeats the sender, for a `spawn` the spawner, for a `fork` the agent copied from (returned as `fork_from`; the sender of a fork is the agent that executed it). The query selects rows whose `agent_id` is among the agents or whose `source` is `agent:N` of one of them (`audit_rows.query_events(involving_agents=...)`).
- **What is left out.** An event whose source is valid but neither an agent nor the user (`system`, `schedule:N`...), and one an agent did to itself. A source with an unknown prefix fails the read (`base.agents.messages.envelope.validate_source`) rather than being skipped. A `send_message` is returned only when its `inbound_id` names a `kind='chat'` inbound row (read per page from `inbound_messages`); a task assignment is a system note and is not drawn. `cancel`, heartbeats and the `self.*` events are not link kinds.
- **Notices.** Also returned: the rows of `agent_notices` the agents created in the window, as `kind: "notice"` with `receiver: null` (the user) and the title as `preview`. They are the one structured agent-to-user channel; nothing else says an agent wrote to the user. A user's chat message has no audit event and is not in this read: the page takes it from the receiver's units.
- **Fields.** `kind`, `ts` (when it was recorded), `sender`, `receiver`, `inbound_id` (the receiver's inbound row when the event was delivered as one, which the units of `run-timeline` carry as `inbound_id`), `fork_from`, `preview` (the start of a message, clipped).
