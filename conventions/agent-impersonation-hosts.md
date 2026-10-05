# External host inbox delivery

An impersonation relay runs beside the external host on the Ava agent's machine.
It subscribes to the agent's existing Redis inbound channel and reads pending
messages from the database. It pushes each pending message's full content into
an already-open host conversation, in self-contained envelopes that carry the
message ids and the exact ACK command; the external agent acknowledges each batch on
receipt and processes messages as they arrive.

This page is the operator and mechanism side. The executor's side of each host
is its host guide under `.agents/skills/impersonator-guide/reference/`
(`claude_code.md`, `codex.md`, `deepseek_harness.md`).

The relay is part of the takeover, not a manual step. The request records the
relay endpoint on the lease row (`--provider`, `--thread-id`, `--codex-remote`).
For codex, the accepting runtime provisions the relay's scoped credential,
spawns the relay at activation and supervises it; for claude and dsh, the relay
runs inside the controller session (see below) and the activation gate verifies
its heartbeat. In both cases a relay that is not live when the takeover would
activate rolls the acceptance back loudly: the lease ends `rejected` with the
reason, the native agent receives a system note and keeps running.

The relay waits natively for preparation and quiescence, then delivers the
session's start message — the `start_message` recorded on the lease during preparation —
even if the inbox is empty. Rejection or expiry also wakes the controller;
waiting for activation never requires a model to poll status.
The relay authenticates with the lease's scoped `relay_token` — never the
controller identity — and no relay session, token or message files are created.
The controller itself holds no credential: see *Control plane* below.

Verify that the start message actually arrives in the intended conversation
before relying on automatic delivery. Lease activation, relay process liveness,
and transport acceptance establish different facts; none establishes host receipt.

## Codex CLI

Give the interactive host a live PTY and keep its stdin open through workspace
trust confirmation. An unattended launch with closed stdin can leave an accepted
Ava lease active without a usable Codex session.

### Control plane — no deliverable credential

Controller authority is the session id plus caller attestation: every control
command (`say` / `inbox` / `ack` / `renew` / `release` / `exec`) must run from a
process that descends from the session's recorded controller tree — the
executor process captured at request time. Nothing travels through the
environment, argv, or files, so there is no shell environment policy, sentinel
value, or supervisor-held credential to manage. The old
`AVA_IMPERSONATION_TOKEN` prescription (with its `shell_environment_policy` /
`shell_snapshot` settings and presence check) is obsolete: a stale or unrelated
process gets a classified refusal (no-anchor / anchor-dead / chain-mismatch)
instead of a credential error. A control-orphaned lease stays parked until the
native side ends it (restart/stop) or its TTL expires. The attestation anchor set is the supported controller ends — currently `codex`, `claude` and `dsh` (`base/agents/impersonation/_impersonation_store.verify_caller`); claude's native install layout (`<install>/claude/versions/<version>`, whose process name is the version) is recognized as its controller, and dsh — a Node program — is recognized as a `node` process whose script (recorded by `base/native_process/ownership.process_metadata` for Node processes) is the `dsh` launcher or `@deepseek-ai/dsh/lib/bin.js`. Extending support to a new controller CLI means extending that set and this list together.

### One app server for TUI and relay

Use one explicitly addressed app server for both the TUI and the relay.
Setting the remote TUI's environment does not configure its server's tools.

```sh
codex app-server --listen unix:///path/to/private/run/codex.sock
```

Create the socket's parent as a private directory and keep this native server
running. The socket carries native IPC; it is not a message file.
Configure the server's sandbox and approval policy for the authorized work.
Connect the interactive TUI to that exact endpoint:

```sh
codex --remote unix:///path/to/private/run/codex.sock -C /path/to/agent/workspace
```

The Ava takeover launcher wires this topology itself for a file-less takeover:
it starts the app server on a private per-generation socket under the cluster's
`run/` directory with the hands-off approval and sandbox policy, starts a
janitor that ends the server when the coding session dies, connects the TUI
with `--remote`, and passes the endpoint into the launch message so the request
records it (`--codex-remote`) and the runtime's relay delivers into the same
server with Steer delivery.

The codex relay needs no manual start: the accepting runtime spawns it at
activation from the recorded spec, handing the scoped relay credential over a
private stdin pipe (`--token-stdin` — the credential never appears in argv,
environment variables or files). If it cannot start, the acceptance rolls back.
A stale heartbeat degrades delivery while executor authority remains bounded
by its original TTL. A birth-confirmed Codex relay is replaced under the same
lease; see *Executor authority and relay recovery*. The manual form below
remains for diagnostics:

```sh
/path/to/checkout/.venv/bin/ava impersonate relay 42 \
  --session 0 --provider codex --thread-id CODEX_SESSION_UUID \
  --codex-remote unix:///path/to/private/run/codex.sock
```

Codex impersonation requires **Steer delivery**. Pending delivery through
`codex queue` waits for the active turn to finish and does not satisfy that
contract. A missing control endpoint, rejected input, or transport failure
stops the relay; messages remain unacknowledged in Ava for the normal handoff.
There is no automatic downgrade to Pending.

Use the endpoint of the server that owns the existing conversation. Without
`--codex-remote`, the CLI resolves the default local daemon socket
(`$CODEX_HOME/app-server-control/app-server-control.sock`) before requesting
control and records that endpoint. If it is absent, the request fails before
acquiring a lease. A socket's existence does not prove it owns the thread;
the first delivery still has to succeed. The relay independently checks for
an endpoint before heartbeating, including requests made through the SDK.

The relay uses `turn/start` as Codex's atomic start-or-steer operation: an idle
thread starts a turn, while an active regular turn receives additional input
inside that turn. This behavior is verified with Codex 0.155.1. The explicit
[`turn/steer` API](https://developers.openai.com/codex/app-server/#steer-an-active-turn)
also steers, but requires the active `expectedTurnId` and fails if the turn
ends between lookup and submission. Using `turn/start` avoids that race without
changing to Pending semantics. Review and manual compaction can refuse input;
that refusal remains a delivery failure.

Select the existing session UUID explicitly (`/status` in the TUI). The relay
never starts `codex exec` or resumes a conversation per message. Desktop,
embedded, and CLI sessions can use different app servers; the same UUID or
shared queue database does not establish ownership or receipt. A host without
a reachable control endpoint cannot support this takeover. Moving the current
conversation to another host is a separate handoff, not a relay fallback.
`--codex-remote` is rejected for Claude Monitor.

Verify receipt on the actual host while it is busy and while idle. Server
acceptance is not host receipt: only the controller's explicit Ava ACK
records that the input reached it. A timeout can follow acceptance, so a failed
transport must not silently resubmit through a second delivery mechanism.

## Claude Code Monitor

Ask the existing Claude session, or the subagent taking the lease, to invoke its
`Monitor` tool with this shape, substituting the executable and identifiers.
Start it immediately after the request: the activation gate requires its
heartbeat. The request response carries the scoped `relay_token`; set it as
`AVA_IMPERSONATION_RELAY_TOKEN` in the Monitor command's environment. If the
Monitor cannot start (no fresh heartbeat), acceptance rolls back loudly.

```json
{
  "command": "AVA_IMPERSONATION_RELAY_TOKEN=<relay token> /path/to/checkout/.venv/bin/ava impersonate relay 42 --session 0 --provider claude",
  "description": "Ava agent 42 inbox",
  "timeout_ms": 1800000
}
```

Each flushed stdout line becomes a notification to the Monitor's owner; the
same subprocess stays subscribed between events, with no repeated LLM polling.
Every watch carries a deadline (`timeout_ms`; 5 minutes when omitted, capped at
30 minutes — arm with 1800000). At the deadline the watch **and the relay
process it runs are killed**, and the session receives one
`[Monitor expired ... Re-arm it if you still need the watch.]` notice. Re-arm as
soon as that notice arrives: the fresh arm starts a relay that resumes delivery
using each message's existing attempt count and ACK deadline. A missed re-arm
leaves delivery visibly degraded; the lease keeps its original TTL. Claude
Monitor requires the controller to re-arm it. The legacy
`persistent` field is ignored; an arm without `timeout_ms` runs under the
5-minute default. Verified on Claude Code 2.1.275 (task #4037). Normal Bash
permissions apply. Monitor is unavailable
with third-party model providers or the telemetry-disabling environment options
listed in the [official Monitor reference](https://code.claude.com/docs/en/tools-reference#monitor-tool).
Background subagents retain Monitor in their
[documented tool set](https://code.claude.com/docs/en/sub-agents#available-tools).
Stopping the owner or ending the session stops its monitors. An ordinary
background Bash command does not substitute for Monitor's per-line delivery.

Plugin-declared monitors run for the session lifetime instead of a per-watch
deadline, but they are an experimental component; evaluate them before relying
on the mechanism (task #4037).

MCP Channels are another supported push mechanism, but require startup opt-in
and custom-server preview configuration; this relay uses Monitor directly. See
the [channel protocol](https://code.claude.com/docs/en/channels-reference).

## DeepSeek Harness (dsh)

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`npm
install -g @deepseek-ai/dsh`; verified with 0.1.5-rc.3) has no external input
channel into a live session, so its relay is a Cordis plugin inside the dsh
process: `ava_builtins/skills/coordination/ava-use-other-agents/scripts/ava-relay-dsh/ava-relay.mjs`
(plain ESM on Node built-ins; dsh developer-preview APIs may change). Load it
by absolute path with a patch file, either per launch (`dsh --profile web
--patch ava-relay.patch.yml`; launcher flags precede the app's own) or for
every profile in `$DSH_HOME/cordis.patch.yml`:

```yaml
- insert:
    - id: ava-relay
      name: /path/to/checkout/ava_builtins/skills/coordination/ava-use-other-agents/scripts/ava-relay-dsh/ava-relay.mjs
```

The plugin exports `DSH_AVA_RELAY_STUB` — a per-session path in a private
directory it owns — to every model shell command. `ava impersonate request
--provider dsh`, run from that shell tool, refuses before creating a lease when
the variable is missing, and otherwise writes the scoped relay credential and
this checkout's interpreter to the stub (0600) instead of printing them: dsh
uploads session logs with its model requests by default, so the credential
never enters the model's context. The plugin polls its directory each second,
consumes the stub once, and runs `ava impersonate relay --provider dsh` as a
background job owned by that session (visible to `job_list`, stopped by
`job_kill` or session disposal). The relay prints one JSON string per push; the
plugin steers each into the session — an idle session starts a turn, a busy one
takes it at its next step, the Steer semantics of the codex relay. There is
nothing for the executor to arm; a missing heartbeat rejects the takeover like
any controller-session relay at admission. Once active, a stale relay degrades
delivery without re-provisioning; executor authority keeps its original TTL.

The executor's commands run from dsh's shell tool, a direct child of the `node`
process, so the attestation anchor is always in reach. Under dsh's default
`workspace-write` sandbox the control commands (request, say, ack, release)
ran in verification; work that writes outside the workspace needs a wider
permission preset. A self-takeover launched by `spawn_dsh.py` runs with
`DSH_PERMISSION_MODE=danger-full-access` because nobody answers approval
prompts in its PTY. dsh resolves its own model credential (environment,
`$DSH_HOME/.credentials.yaml`, the working directory's `.env`, `$DSH_HOME/.env`).

## Executor authority and relay recovery

Confirmed executor death ends authority; relay failure only degrades delivery.
The accepting runtime re-checks both on the held-controls pass, which the dispatcher's
database-backed pending scan triggers for held rows every ~30 seconds — the
check is pull-based and never depends on wake delivery. Worst-case detection is
the stale window plus one scan interval (≈75 s).

- **Executor authority.** Confirmed death/reuse of every recorded provider
  anchor ends the lease. Unknown or absent evidence is visible degradation,
  never confirmed death; authority ends at its original TTL if no decision arrives.
- **Relay transport.** A stale Codex relay is retired by its recorded process
  birth before a locked generation CAS mints a replacement. Its private token
  pipe stays closed until the new child's birth is persisted. No TTL renewal or
  new takeover occurs. A legacy generation-zero relay without a recorded birth
  cannot safely be adopted or retired after host restart: recovery is withheld
  with explicit degradation until an operator ends that old lease. Controller-
  session providers lack independent respawn support and remain visibly degraded.
- **What stopping does.** The lease goes terminal (`expired`) with the cause
  recorded as `aborted: <detail>` in `rejection_reason` (the request's own
  `reason` is preserved), pending renewal reminders are dismissed, a relay
  process this runtime still holds is terminated, and the native agent
  resumes: the end-of-session note names the cause ("This session was stopped
  early: …"), and the `impersonation_aborted` event carries the dead component
  and its detail. A fresh takeover then needs a fresh request.

## Delivery and recovery

- Redis is a latency optimization. The native process also catches up from the
  database every 30 seconds and after reconnect/wake, without invoking an LLM.
  It subscribes before its first delivery snapshot to close the startup race.
- Push with an ACK window: every pending inbox row is pushed once with its
  full content in one envelope per batch. Cluster config
  `AVA_IMPERSONATION_ACK_WINDOW_SECONDS` (default **180**) controls the ACK
  window, and `AVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS` (default **2**) counts
  total attempts including the first submission. Both are positive integers
  configured through the normal config panel/CLI. The request snapshots them
  on the lease; edits apply to new leases, while existing leases and relay
  restarts keep their saved policy. Pre-migration leases keep their 300-second
  window. Each envelope states the window and per-message attempt number.
  Missing the final ACK window pauses that message's automatic delivery.
  Its durable body remains inbox-readable and ACK-able while the lease is active.
  Reads and native reconciliation check this across the whole lease, regardless
  of inbox pagination or new arrivals, on the existing 30-second catchup cycle.
  Due retries take priority over fresh rows. Rows already pending at activation push immediately
  (they waited through preparation); fresh routine arrivals coalesce inside the
  lease's configured merge window, stated explicitly at request time
  (0..300 seconds; 0 pushes immediately), while user chats, cancels and renewal
  reminders never wait. New messages arriving under an
  outstanding batch push as their own batch.
- Pushes are debounced (default 0.5 seconds, maximum 30) and emitted at most
  once every two seconds. Terminal control notices are immediate. Every relay
  provider truncates each content block to 2000 characters with a pointer to
  the inbox command: it fits Claude Monitor's per-line budget and keeps the
  host input bounded, so one oversized inbound cannot fail
  every emit and wedge the relay. Fetch messages (or full payloads) with
  `impersonate inbox 0 --agent 42` — a truncated body is fetched in full
  before its ACK; acknowledge each batch on receipt with
  `impersonate ack 0 ID ... --agent 42`. The envelope's ACK line carries the
  exact command for its batch.
  An ACK that marks messages done (the record's delivery status — not task
  completion) publishes a wake so the relay immediately drops the ids from its
  outstanding set, even when no new message has arrived.
  Repeating an ACK for already-done messages does not publish another wake.
  Treat `kind="cancel"` as a request to stop current work; acknowledge it on receipt (like every message), then stop.
  Native Ava does not consume cancellation on behalf of the external controller.
- Reading or successfully submitting a push does not mark a message done. The
  relay reserves each attempt in the database before host submission. Restart
  and credential rotation preserve the attempt count and ACK deadline. Failed
  or ambiguous submissions spend an attempt because transport acceptance and
  database commit cannot be atomic; content remains available for native
  handoff. Envelope ids remain the idempotency key: a host that already received
  a batch simply re-ACKs it. The configured budget is per message, with no
  exactly-once claim across transport or process crashes.
- The relay heartbeats every 10 seconds; a beat stale past 45 seconds triggers
  transport recovery, not identity expiry. Same-lease recovery preserves attempt
  budgets and reservations, and cannot overlap an unconfirmed old sender.
- The machine host's existing scan delivers newly ended leases' notices using
  immutable recorded Codex endpoint/thread snapshots, including after native
  restoration, replacement takeover or agent termination. Each RPC is bounded;
  transient failures retry with backoff capped at fifteen minutes. Acceptance is
  host acceptance, not executor receipt; ambiguous timeout can duplicate a notice.
  Unsupported destinations retain explicit failure and are never called delivered.
  Notices name the old lease and never cancel a replacement. Native handoff does
  not depend on notice delivery; pre-migration ended leases are not backfilled.
- Renewal reminders: five minutes before a lease expires, the gateway inserts
  a durable inbox row of `kind="reminder"` (one per expiry deadline; the payload carries
  the session linkage) that the relay pushes like any message. Release or expiry
  dismisses any still-pending reminder, so the native agent never sees a stale
  one.
- Release, expiry, rejection, an invalid lease, a failed host delivery or a broken
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

Steer input reaches the active turn at its next processing opportunity; it
does not promise to interrupt an in-flight tool. Transport success also
does not prove that the model processed the message. Keep processing ACKs in
Ava, and make actions safe to retry when their completion is ambiguous.

The retired `AVA_IMPERSONATION_REPROVISION_WINDOW_SECONDS` setting is ignored
by existing environment and saved-overlay readers; new configuration writes
reject it. Removing an old host setting is a separate operator action.
