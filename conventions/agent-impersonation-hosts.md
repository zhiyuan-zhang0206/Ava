# External host inbox delivery

An impersonation relay runs beside the external host on the Ava agent's machine.
It subscribes to the agent's existing Redis inbound channel and reads pending
messages from the database. It pushes each pending message's full content into
an already-open host conversation, in self-contained envelopes that carry the
message ids and the exact ACK command; the external agent processes messages
as they arrive and ACKs by id.

The relay is part of the takeover, not a manual step. The request records the
relay endpoint on the lease row (`--provider`, `--thread-id`, `--codex-remote`).
For codex, the accepting runtime provisions the relay's scoped credential,
spawns the relay at activation and supervises it; for claude, the relay runs
inside the controller session (see below) and the activation gate verifies its
heartbeat. In both cases a relay that is not live when the takeover would
activate rolls the acceptance back loudly: the lease ends `rejected` with the
reason, the native agent receives a system note and keeps running.

The relay waits natively for consent and quiescence, then delivers the
native agent's start message — the handoff brief recorded at acceptance —
even if the inbox is empty. Rejection or expiry also wakes the controller;
waiting for a decision never requires a model to poll status.
The relay authenticates with the lease's scoped `relay_token` — never the
controller's `AVA_IMPERSONATION_TOKEN` — and no relay session, token or
message files are created.

Verify that the start message actually arrives in the intended conversation
before relying on automatic delivery. Lease activation, relay process liveness,
and queue acceptance establish different facts; none establishes host receipt.

## Codex CLI

Give the interactive host a live PTY and keep its stdin open through workspace
trust confirmation. An unattended launch with closed stdin can leave an accepted
Ava lease active without a usable Codex session.

Use one explicitly addressed app server for both the TUI and the relay. Pass the
token through that **server's** environment, with an explicit shell policy;
setting the remote TUI's environment does not configure its server's tools.
The environment policy was verified with Codex 0.153.4; its
[environment filtering order](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/shell_environment.rs)
and [snapshot implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/shell_snapshot.rs)
explain the required settings:

```sh
codex --disable shell_snapshot \
  -c 'shell_environment_policy.inherit="all"' \
  -c 'shell_environment_policy.ignore_default_excludes=true' \
  -c 'shell_environment_policy.include_only=["PATH","HOME","USER","LOGNAME","SHELL","TERM","LANG","LC_ALL","TMPDIR","AVA_IMPERSONATION_TOKEN","CODEX_THREAD_ID"]' \
  app-server --listen unix:///path/to/private/run/codex.sock
```

Create the socket's parent as a private directory and keep this native server
running. The socket carries native IPC; it is not a token or message file.
Configure the server's sandbox and approval policy for the authorized work.
Connect the interactive TUI to that exact endpoint:

```sh
codex --remote unix:///path/to/private/run/codex.sock -C /path/to/agent/workspace
```

Use these options together. `inherit="core"` removes the token before
`include_only` runs, so an allowlist alone cannot restore it. Default secret-name
exclusions also remove the token; disabling those exclusions requires the strict
allowlist above. Add other environment names only when the host needs them.
Keep shell snapshots disabled: a snapshot can persist the inherited credential
and hide a missing subprocess environment until the working directory changes.

Before requesting a live lease, test the policy with a harmless sentinel value
for `AVA_IMPERSONATION_TOKEN`. Have Codex run this presence check through its own
shell tool both in the agent workspace and in the intended Ava checkout; repeat
it with the real inherited credential before Ava work:

```sh
python3 -c 'import os; assert os.environ.get("AVA_IMPERSONATION_TOKEN"), "Ava impersonation token missing"'
```

The check must succeed in both directories without printing the token. Capture
the request response in the supervisor; keep the credential out of prompts,
command arguments, logs and token files. Retain that in-memory copy until handoff
completes, so `finally` cleanup can release an active lease through the Ava CLI
even if the host never starts, loses stdin or cannot inherit the token.
Stop external work and close attachments before releasing; verify the terminal
lease status before discarding the supervisor's credential. TTL remains the
recovery path if the supervisor dies.

The codex relay needs no manual start: the accepting runtime spawns it at
activation from the recorded spec, handing the scoped relay credential over a
private stdin pipe (`--token-stdin` — the credential never appears in argv,
environment variables or files). If it cannot start, the acceptance rolls back.
The runtime respawns it when its heartbeat goes stale while the lease is
active. The manual form below remains for diagnostics:

```sh
/path/to/checkout/.venv/bin/ava impersonate relay 42 \
  --lease-id LEASE_UUID --provider codex --thread-id CODEX_SESSION_UUID \
  --codex-remote unix:///path/to/private/run/codex.sock
```

The adapter invokes `codex queue --thread UUID --message TEXT --remote ENDPOINT`, preserving the
existing conversation. It never starts `codex exec` or resumes a conversation
per message. Select the session UUID explicitly; `/status` in that CLI session
shows it. The `codex` executable on PATH must support `queue` and reach the same
app server as that session. Run `codex queue --help` to check the
installed command. Codex 0.149.0 introduced the queue command and idle-session
wake behavior; see the [official changelog](https://developers.openai.com/codex/changelog/).

The endpoint is optional for existing setups that already share a server, but
the UUID alone does not select the process holding the session. In Codex 0.153.4,
CLI configuration overrides can select an embedded server while a separate
queue command reaches another server. The owning server then discovers the
external queue write on a **10-second interval**, adding up to roughly ten
seconds before it starts an idle turn. Queue submission to that owning server
instead calls its wake path immediately; see the tagged
[server selection](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/tui/src/lib.rs)
and [queue dispatch](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/queue/src/service.rs)
implementations. Both TUI and relay must use the same endpoint. A successful
queue command means accepted delivery, not that the model has started or ACKed
the Ava message. `--codex-remote` is rejected for Claude Monitor.

Desktop and ChatGPT embedded sessions may use a different app-server instance
or event consumer; a thread UUID alone does not establish delivery. Idle wake-up
has also been verified in a ChatGPT embedded host through the shared CLI queue:
a queued push started a new turn after the active turn ended. Busy-turn delivery
is not implied. Test receipt both while the host is busy and after it becomes
idle; queued items during an active turn alone do not establish a delivery
failure. Do not infer receipt from a shared database or a zero queue exit code.
Verify the existing conversation's behavior before changing hosts; moving work
into a CLI session is a separate host handoff.

## Claude Code Monitor

Ask the existing Claude session, or the subagent taking the lease, to invoke its
`Monitor` tool with this shape, substituting the executable and identifiers.
Start it immediately after the request: the activation gate requires its
heartbeat. The request response carries the scoped `relay_token`; set it as
`AVA_IMPERSONATION_RELAY_TOKEN` in the Monitor command's environment. If the
Monitor cannot start (no fresh heartbeat), acceptance rolls back loudly.

```json
{
  "command": "AVA_IMPERSONATION_RELAY_TOKEN=<relay token> /path/to/checkout/.venv/bin/ava impersonate relay 42 --lease-id LEASE_UUID --provider claude",
  "description": "Ava agent 42 inbox",
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
- Push with an ACK window: every pending inbox row is pushed once with its
  full content in one envelope per batch, and every unacknowledged batch is
  pushed again after five minutes, marked as re-delivery, until the host ACKs
  it or the lease ends. Rows already pending at activation push immediately
  (they waited through consent); fresh routine arrivals coalesce inside the
  lease's configured merge window (default 30 seconds), while user chats,
  cancels and renewal reminders never wait. New messages arriving under an
  outstanding batch push as their own batch.
- Pushes are debounced (default 0.5 seconds, maximum 30) and emitted at most
  once every two seconds. Terminal control notices are immediate. Every relay
  provider truncates each content block to 2000 characters with a pointer to
  the inbox command: it fits Claude Monitor's per-line budget and keeps the
  codex `queue --message` argv bounded, so one oversized inbound cannot fail
  every emit and wedge the relay. Fetch messages (or full payloads) with
  `impersonate inbox LEASE_UUID`; process and explicitly
  `impersonate ack LEASE_UUID ID ...`. The envelope's ACK line carries the
  exact command for its batch.
  An ACK that marks messages done publishes a wake so the relay immediately
  drops the ids from its outstanding set, even when no new message has arrived.
  Repeating an ACK for already-done messages does not publish another wake.
  Treat `kind="cancel"` as a request to stop current work, then explicitly ACK it.
  Native Ava does not consume cancellation on behalf of the external controller.
- Reading or successfully queueing a push does not mark a message done. The
  relay tracks pushed-but-unacknowledged ids in memory. Restart replays every
  still-pending row it encounters. This is at-least-once delivery, with the
  envelope ids as the idempotency key: a host that already handled a batch
  simply re-ACKs it. There is no exactly-once claim across provider
  acknowledgement or process crashes.
- The relay heartbeats the lease row every 10 seconds; a heartbeat older than
  45 seconds counts as stale. While the lease is active, the accepting runtime
  respawns a dead codex relay on the next claim wake (at most once a minute),
  and every inbound wake checks the heartbeat and logs loudly when it is stale,
  so messages never sit silently. A claude relay cannot be respawned from the
  native side; a stale heartbeat is stamped on the lease row
  (`relay_last_failure_at`, visible in `impersonate status`) and logged.
- Renewal reminders: five minutes before a lease expires, the gateway inserts
  a durable inbox row of `kind="reminder"` (one per lease; the payload carries
  the lease id) that the relay pushes like any message. Release or expiry
  dismisses any still-pending reminder, so the native agent never sees a stale
  one.
- Release, expiry, rejection, an invalid lease, a failed host queue or a broken
  Monitor pipe stops delivery. Expiry sends a loss-of-control notice before
  stopping; the database's clock and status decide authority. A local clock
  difference only adjusts the next native status check. Pending messages remain
  in the database. Interruption
  closes the subscriber without releasing or extending the lease.
  On release or expiry the native runtime also kills the relay process it
  spawned — the teardown is symmetric with activation. Inbox emptiness or
  subtask completion never stops delivery: only a terminal lease status does.
- The relay **never renews a lease**. Renewal is an explicit controller action;
  TTL remains the recovery boundary if the controller or its relay dies.
  Native resume still requires the lease lifecycle's normal handoff checks.

The host receives a queued event at its next processing opportunity; there is
no promise to interrupt a token or an in-flight tool. Transport success also
does not prove that the model processed the message. Keep processing ACKs in
Ava, and make actions safe to retry when their completion is ambiguous.
