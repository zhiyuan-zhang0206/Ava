# External agent impersonation

An external Codex or Claude Code session can request an Ava agent's identity on
that agent's machine. The native agent explicitly accepts or rejects. Acceptance
ends its current code execution; the lease becomes active only after its execution
resources close and its checkpoint flushes. While active, the native model stays
paused and incoming messages remain available to the external controller.

## Request and consent

Use the `ava` executable belonging to the intended cluster. A bare `ava` on PATH
usually belongs to production; a worktree's `.venv/bin/ava` belongs to its own
cluster. Direct Python tools must use that installation's interpreter too.

```bash
ava impersonate request --agent 405 --as codex:work405 --ttl 3600 \
  --provider codex --thread-id CODEX_SESSION_UUID \
  --reason 'Handle the outstanding implementation and return a summary'
```

Every request must name its relay endpoint up front with `--provider`
(`codex` or `claude`; `codex` also requires `--thread-id`, the existing
session UUID the relay queues into, and accepts `--codex-remote`). A request
without a relay binding is rejected at request time; the native side never
guesses an endpoint.

The JSON response contains the lease `id` and its newly minted `token`. The
token appears only in that request response; subsequent commands read it from
the process environment. A `claude` request additionally returns a scoped
`relay_token` for the relay process. No external conversation or token files
are created.

```bash
export AVA_IMPERSONATION_TOKEN='<request token>'
ava impersonate status '<lease id>'
```

An export persists only in that shell and its children. If the external host starts
a new shell for each tool call, supply the token in each Ava subprocess environment,
use a persistent shell, or start the host with the token already in its environment.
Codex also filters subprocess environments; use the explicit
[Codex launch policy and presence check](agent-impersonation-hosts.md#codex-cli)
before doing Ava work.

The [host relay](agent-impersonation-hosts.md) is part of the takeover, not a
manual step. The accepting runtime starts the codex relay automatically at
activation; the claude relay starts inside the controller session right after
the request (its `relay_token` travels in `AVA_IMPERSONATION_RELAY_TOKEN`).
Activation gates on the relay's heartbeat: if the relay cannot start, the
acceptance rolls back loudly — the lease becomes `rejected` with the reason,
the native agent is told, and it keeps running. There is no silent
half-takeover.

The native agent receives the request in its normal context and decides:

```python
import ava
ava.impersonation.accept(
    request_id,
    start_message="Brief for the external session: the current work, the "
    "context it needs, and how to acknowledge incoming messages.",
)  # Ends this code execution immediately. The start message is required and
# is delivered to the external session when the takeover starts.
# Or: ava.impersonation.reject(request_id, reason="Finish the current operation first")
```

`requested` means consent is pending; `accepted` means execution is draining;
`active` means the external controller may act. `rejected`, `released`, and
`expired` are terminal. TTL is explicit, from 1 through 86400 seconds, and an active
lease is renewed only by `ava impersonate renew '<lease id>' --ttl 3600`.
A new request is required after a lease ends.

## Read context

```bash
ava agents context 405
ava agents timeline 405 --limit 100 --before 23.0
```

`context` is an alias for `timeline`: both return the existing timeline API as
JSON. Its default window includes the latest 50 rendered items plus the system
prompt, standing memory/identity/skill notes, and compact summaries. Use `has_more`
and item cursors to read older history. This works without impersonating the agent.
The API preserves its existing UI read behavior: a checkpoint read failure can
produce an empty view, so an empty result alone does not establish an empty agent.

`ava.agents.get_status()` still reports the parked native runtime, which can be
`idling` while the external host works. Existing idle watchers do not track
external model turns. Use explicit completion messages or the return handoff
to judge external work; host busy/idle mirroring is outside this first version.

## Use the agent's SDK from local Python

The external model continues to use its own native tools. Where Ava context or
capabilities are useful, attach a short Python operation to the approved lease:

```python
import os
import ava

with ava.external.attach(lease_id, token=os.environ["AVA_IMPERSONATION_TOKEN"]):
    print(ava.self.AGENT_ID)
    ava.help(ava)
    ava.agents.send_message(other_agent_id, "The implementation is ready for review")
```

The attachment loads that agent's plugins, pinned configuration and checkpoint.
SDK identity resolution, MCP dispatch, plugin state handles and attachment flush
revalidate the active lease and local machine. Raw `ava.state` is a local snapshot;
reading its fields does not query the lease. Peer messages and spawns carry the
borrowed `agent:N` identity. Outside
an attachment, an explicit `AVA_CALLER_IDENTITY` profile continues to identify the
external tool as itself.

The context manager stages plugin state on exit. A longer attachment can call
`attachment.flush()` between steps; this does not renew the lease. Native execution
remains the sole checkpoint writer and applies staged deltas before resuming.
Message updates may append to the existing history under the normal reducer rules.
External deltas cannot clear or rebuild the full history with
`RemoveMessage(REMOVE_ALL_MESSAGES)`; that operation is reserved for native
compaction and crash repair, and is rejected before journaling or replay.
Concurrent attachments detect conflicting state versions and fail rather than
silently overwriting one another. SDK calls already in progress are ordinary
external process work; expiry prevents further validated calls.

Use `ava.agents.restart(ava.self.AGENT_ID)` or
`ava.agents.terminate(ava.self.AGENT_ID)` for durable lifecycle requests. These
commands reach the native lifecycle dispatcher while it is paused. Flush pending
plugin updates before a lifecycle request. The `ava.self.restart`, `terminate`
and `compact` forms end an owning native execution,
so they remain native-loop operations. Include any required native compaction or
configuration change in the release summary. Python objects, direct database
access and the external model's own shell/editor tools are outside this cooperative
SDK lease guard.

For shell-driven use, `ava impersonate exec '<lease id>' --file operation.py`
executes the file in the local Python process with an attachment. Omit `--file`
to read Python from stdin. This command does not ask the native Ava model to run
or generate the code.

## Receive and acknowledge messages

Messages are pushed into the external session by the bound
[host relay](agent-impersonation-hosts.md): each push is one self-contained
envelope carrying the full message bodies, their ids, and the exact ACK
command. Process the batch, then acknowledge only the ids actually handled:

```bash
ava impersonate ack '<lease id>' 123 124
```

A batch that is not acknowledged within five minutes is pushed again, marked
as re-delivery, until it is ACKed or the lease ends. The ids make delivery
idempotent: re-ACKing an already handled batch is harmless, and the relay
never acknowledges work for the external model. Unacknowledged rows remain
available after release.

`ava impersonate inbox '<lease id>'` remains the fallback read (`--limit`
1..1000 rows; `--wait` finite, nonnegative seconds); use it for missed or
truncated content and for message payloads, which pushes do not carry.
The relay heartbeats the lease row; while the lease is active, the accepting
runtime respawns a dead codex relay and every inbound delivery path checks
the heartbeat and alerts loudly when it is stale, so messages never silently
sit in the inbox.

An inbox row with `kind="cancel"` asks the controller to stop its current
work. Stop that work and explicitly ACK the request; an unacknowledged cancel
remains for native processing when control returns. This reaches the external
model at its next processing opportunity. To interrupt an in-flight external
tool immediately, use that Codex or Claude session's own stop control.

A row with `kind="reminder"` is a lease-expiry renewal reminder sent by Ava
five minutes before the TTL elapses; renew or release before expiry, and ACK
it like any message.

## Return control

Close active Python attachments before releasing the lease, then hand back a concrete summary:

```bash
ava impersonate release '<lease id>' \
  --summary 'Implemented X and verified Y. Z remains open; resume with its failing case.'
```

Release durably queues the summary as the session's end message with the
external controller's real provenance.
The native runtime applies staged plugin state and resumes from its checkpoint,
killing the bound relay process it started at activation. TTL expiry also returns
control, while preserving unprocessed inbound messages. Completing a subtask is
not release authority: the lease (and the relay) stay until the explicit release
or expiry. An external session ending does not require a new model conversation
or a transcript file: the native agent retains its own context and receives the
explicit handoff.

A lease whose relay could not be established is never active: it ends as
`rejected` with the reason in `rejection_reason`, the native agent receives a
system note, and it keeps running. `ava impersonate status` shows the relay
fields (`relay_provider`, `relay_heartbeat_at`, `relay_last_failure_at`) for
forensics.
