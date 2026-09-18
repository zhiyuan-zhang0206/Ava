# External agent impersonation

A trusted Codex or Claude Code process on an Ava agent's machine can take over
its identity. Preparation drains native work and saves its checkpoint before
activation. It does not ask the native model to approve. TTL is an explicit
recovery deadline, for renewal and the initial takeover alike — estimate it
short: take the smallest window that covers the next slice (about 30 minutes
when the work ahead looks like about an hour) and extend by renewal. A short
window is the deadlock backstop; a long one parks the agent for its whole
span if the external side dies.

## Start a named session

Use the executable and interpreter belonging to the intended checkout; bare
`ava` on PATH usually belongs to production.

```bash
ava impersonate request --agent 405 --name 'Fix login' --as 'Codex: login helper' \
  --provider codex --thread-id CODEX_SESSION_UUID --ttl 3600 \
  --reason 'Implement the login fix and return verification results'
```

`--name` describes this session; `--as` is a free executor display name. The CLI
also records observed process names, IDs, executable and parent chain, separately
from the declaration. `--provider` selects `codex` or `claude` relay transport.
Codex's own thread UUID is a provider address, not the Ava session handle.
Preserve `CODEX_HOME` and optionally supply `--codex-remote` so the native relay
reaches the owning server. Claude starts its Monitor relay immediately after the
request. See [host setup](agent-impersonation-hosts.md).

The response returns a per-agent integer `id` / `session_id`, starting at zero.
There is no controller credential: control commands are authorized by the
session id plus caller attestation — each command must run from a process that
descends from the session's recorded controller tree (the executor process that
made the request). Claude's relay uses the separate scoped
`AVA_IMPERSONATION_RELAY_TOKEN`; that credential belongs to the relay, never to
the controller. Status/list responses omit credentials.

```bash
ava impersonate status 0 --agent 405
ava impersonate list --agent 405 --limit 20
ava impersonate list --agent 405 --before 20 --limit 20
```

Wait for `active`. `preparing` means native work or relay readiness is still
settling. `released`, `expired` and `rejected` are terminal. A fresh request
cannot overlap an open session or a pending native handoff. IDs never reset or
get reused. Names may repeat; the pair `(agent_id, session_id)` identifies work.

## Read context and act

```bash
ava agents context 405
ava agents timeline 405 --limit 100 --before 23.0
ava impersonate exec 0 --agent 405 --file operation.py
```

The exec form runs local Python in a short attachment. Direct Python uses:

```python
import ava

with ava.external.attach(0, agent_id=405):
    ava.agents.send_message(406, "Please review the login change")
```

User-visible replies never go through the attachment — send them with the CLI (`ava impersonate say`, see *Talk to the human*).

An attachment binds the borrowed identity and saved configuration. SDK calls,
plugin state accesses and flush validate the active lease. Plugin changes are
journaled; the native graph applies them before resuming. Direct Python object
reads and native shell/editor actions are outside this cooperative guard.
Concurrent attachments fail on conflicting state versions. External deltas cannot
clear all message history. `ava.self.restart`, `terminate` and `compact` remain
native-loop operations; include such needs in the release summary. Durable
`ava.agents.restart` / `terminate` requests still reach the paused dispatcher.

## Talk to the human

```bash
ava impersonate say 0 --agent 405 --key progress-1 'I found the cause.'
ava impersonate say 0 --agent 405 --key question-1 'Should the empty state offer sign-in?'
ava impersonate say 0 --agent 405 --key final-1 --phase final 'The fix is verified.'
```

These replies appear immediately on the ordinary Ava timeline with executor and
session metadata. Reuse a key only for retrying identical content. A repeated key
with different content fails. The logical sender is the Ava agent; incoming user
messages retain their direction and sender. No frontend message table is split.

## Receive, ACK and renew

The relay pushes full inbound content, IDs and the exact ACK command. Process
only the work you actually handled, then acknowledge it:

```bash
ava impersonate ack 0 123 124 --agent 405
ava impersonate inbox 0 --agent 405 --limit 100
ava impersonate renew 0 --agent 405 --ttl 3600
```

Inbox reads do not ACK. Missed batches repeat after five minutes. ACK changes
processing state, never history retention. `cancel` asks the external controller
to stop; ACK after stopping. `reminder` indicates an approaching TTL deadline:
decide whether to renew once or release. Renew explicitly, never in an automated
heartbeat loop. TTL is 1..86400 seconds; relay liveness does not extend it.

Messages are model work — a delivered message wakes a model on its receiving
side (tokens, not free): send substantive traffic (work, blockers, questions),
not chatter; routine status belongs in the release summary.

## Return control

Stop external work and close attachments before releasing:

```bash
ava impersonate release 0 --agent 405 \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary must be written by the impersonator. Ava generates one JSON file at
`<agent workspace>/impersonation/0.json`, with session/process metadata, all
incoming/outgoing messages, inbound ACK state, lifecycle history, original
consumed SDK/API events and counts (calls, task changes, recipients, duration).
The file is saved before native execution resumes. The summary and file path
arrive as the first new system note, ahead of subsequent normal input. Captured
unacknowledged input remains in the JSON for the native agent to handle.

Expiry or failed activation also produces a handoff, explicitly stating the
reason and absence of an external summary. Session history is permanent,
including timestamps, renewals, ACKs and message bodies; exports can be rebuilt.
SDK statistics come from the existing event collector. This feature consumes
those facts and does not change collection or sampling.
