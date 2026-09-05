# External host inbox delivery

An impersonation relay runs beside the external host on the AVA agent's machine.
It subscribes to the agent's existing Redis inbound channel and reads pending
messages from the database. It sends only short inbox hints to an already-open
host conversation; the external agent fetches and processes the full messages.

Start the relay immediately after making an impersonation request. It waits
natively for consent and quiescence, then sends one control-active hint even if
the inbox is empty. That hint directs the controller to the agent's existing
timeline/context and inbox. Rejection or expiry also wakes the controller;
waiting for a decision never requires a model to poll status. Pass the lease
UUID explicitly and inherit its credential as `AVA_IMPERSONATION_TOKEN`.
No relay session, token or message files are created.
Use the absolute AVA executable belonging to the intended cluster: a bare `ava`
on PATH can point to production even when the current directory is a worktree.

## Codex CLI

Start the relay as a background shell process owned by the external session:

```sh
/path/to/checkout/.venv/bin/ava impersonate relay 42 \
  --lease-id LEASE_UUID --provider codex --thread-id CODEX_SESSION_UUID
```

The adapter invokes `codex queue --thread UUID --message TEXT`, preserving the
existing conversation. It never starts `codex exec` or resumes a conversation
per message. Select the session UUID explicitly; `/status` in that CLI session
shows it. The `codex` executable on PATH must support `queue` and reach the same
local app-server daemon as that session. Run `codex queue --help` to check the
installed command. Codex 0.149.0 introduced the queue command and idle-session
wake behavior; see the [official changelog](https://developers.openai.com/codex/changelog/).

The CLI daemon's session namespace is the tested destination. A Desktop session
may use a different app-server instance; its UUID alone does not establish that
the CLI can reach it. Use a CLI session when that connection is unavailable.

## Claude Code Monitor

Ask the existing Claude session, or the subagent taking the lease, to invoke its
`Monitor` tool with this shape, substituting the executable and identifiers:

```json
{
  "command": "/path/to/checkout/.venv/bin/ava impersonate relay 42 --lease-id LEASE_UUID --provider claude",
  "description": "AVA agent 42 inbox",
  "persistent": true
}
```

Each flushed stdout line becomes a notification to the Monitor's owner. The
same subprocess stays subscribed between events; no repeated LLM polling or
shell restart is needed. Normal Bash permissions apply. Monitor is unavailable
with third-party model providers or the telemetry-disabling environment options
listed in the [official Monitor reference](https://code.claude.com/docs/en/tools-reference#monitor-tool).
Background subagents retain Monitor in their
[documented tool set](https://code.claude.com/docs/en/sub-agents#available-tools).
Stopping the owner or ending the session stops its monitors. An ordinary
background Bash command does not substitute for Monitor's per-line delivery.

MCP Channels are another supported push mechanism, but require startup opt-in
and custom-server preview configuration; this relay uses Monitor directly. See
the [channel protocol](https://code.claude.com/docs/en/channels-reference).

## Delivery and recovery

- Redis is a latency optimization. The native process also catches up from the
  database every 30 seconds and after reconnect/wake, without invoking an LLM.
  It subscribes before its first delivery snapshot to close the startup race.
- Inbox hints are debounced (default 0.5 seconds, maximum 30) and emitted at most
  once every two seconds. Terminal control notices are immediate. Claude Monitor
  truncates and rate-limits output, so message bodies
  never travel through its stdout channel. Fetch messages using `impersonate
  inbox LEASE_UUID`; process and explicitly `impersonate ack LEASE_UUID ID ...`.
  Drain inbox pages until empty before waiting again.
- Reading or successfully queueing a hint does not mark a message done. The
  relay suppresses repeated hints for the same pending page in memory. Restart
  replays every still-pending page it encounters. This is at-least-once delivery,
  with no exactly-once claim across provider acknowledgement or process crashes.
- Release, expiry, rejection, an invalid lease, a failed host queue or a broken
  Monitor pipe stops delivery. Expiry sends a loss-of-control notice before
  stopping; the database's clock and status decide authority. A local clock
  difference only adjusts the next native status check. Pending messages remain
  in the database. Interruption
  closes the subscriber without releasing or extending the lease.
- The relay **never renews a lease**. Renewal is an explicit controller action;
  TTL remains the recovery boundary if the controller or its relay dies.
  Native resume still requires the lease lifecycle's normal handoff checks.

The host receives a queued event at its next processing opportunity; there is
no promise to interrupt a token or an in-flight tool. Transport success also
does not prove that the model processed the message. Keep processing ACKs in
AVA, and make actions safe to retry when their completion is ambiguous.
