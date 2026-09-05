# External host inbox delivery

An impersonation relay runs beside the external host on the AVA agent's machine.
It subscribes to the agent's existing Redis inbound channel and reads pending
messages from the database. It sends only short inbox hints to an already-open
host conversation; the external agent fetches and processes the full messages.

Start the relay immediately after making an impersonation request. It waits
natively for consent and quiescence, then sends one control-active hint even if
the inbox is empty. That hint directs the controller to the inbox and to any
relevant context it has not already loaded. It gives independent complete inbox
and ACK commands; do not append them to the `agents timeline` command.
Rejection or expiry also wakes the controller;
waiting for a decision never requires a model to poll status. Pass the lease
UUID explicitly and inherit its credential as `AVA_IMPERSONATION_TOKEN`.
No relay session, token or message files are created.
Use the absolute AVA executable belonging to the intended cluster: a bare `ava`
on PATH can point to production even when the current directory is a worktree.

## Codex CLI

Give the interactive host a live PTY and keep its stdin open through workspace
trust confirmation. An unattended launch with closed stdin can leave an accepted
AVA lease active without a usable Codex session.

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
shell tool both in the agent workspace and in the intended AVA checkout; repeat
it with the real inherited credential before AVA work:

```sh
python3 -c 'import os; assert os.environ.get("AVA_IMPERSONATION_TOKEN"), "AVA impersonation token missing"'
```

The check must succeed in both directories without printing the token. Capture
the request response in the supervisor; keep the credential out of prompts,
command arguments, logs and token files. Retain that in-memory copy until handoff
completes, so `finally` cleanup can release an active lease through the AVA CLI
even if the host never starts, loses stdin or cannot inherit the token.
Stop external work and close attachments before releasing; verify the terminal
lease status before discarding the supervisor's credential. TTL remains the
recovery path if the supervisor dies.

Start the relay as a background shell process owned by the external session:

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
the AVA message. `--codex-remote` is rejected for Claude Monitor.

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
  An ACK that marks messages done publishes a wake so the relay immediately
  discovers the next pending page, even when no new message has arrived.
  Repeating an ACK for already-done messages does not publish another wake.
  Treat `kind="cancel"` as a request to stop current work, then explicitly ACK it.
  Native AVA does not consume cancellation on behalf of the external controller.
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
