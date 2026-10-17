# External agent impersonation

A trusted Codex, Claude Code, Antigravity or DeepSeek Harness (dsh) process on
an Meiva agent's machine can take over its identity. Preparation drains native
work and saves its checkpoint before activation. It does not ask the native
model to approve. TTL is an explicit
recovery deadline, for renewal and the initial takeover alike — estimate it
short: take the smallest window that covers the next slice (about 30 minutes
when the work ahead looks like about an hour) and extend by renewal. A short
window is the deadlock backstop; a long one parks the agent for its whole
span if the external side dies.

## Start a named session

Run the commands with a bare `meiva`: it acts on the cluster `MEIVA_HOME` names,
else `~/.meiva`. A checkout's own `.venv/bin/meiva` acts on that checkout's
cluster.

```bash
meiva impersonate request --agent 405 --name 'Fix login' --as 'Codex: login helper' \
  --provider codex --thread-id CODEX_SESSION_UUID --ttl 1800 --batch-window 0 \
  --reason 'Implement the login fix and return verification results'
```

The time parameters are explicit, never defaulted: `--ttl` is the lease's
recovery deadline (see above) and `--batch-window` the relay's merge window
(0..300 seconds; 0 delivers routine arrivals immediately) — a missing value is
a usage error before anything runs.

`--name` describes this session; `--as` is a free executor display name. The CLI
also records observed process names, IDs, executable and parent chain, separately
from the declaration. `--provider` selects `codex`, `claude`, `dsh` or `antigravity`
relay transport.
Codex's own thread UUID (`CODEX_THREAD_ID`) and Antigravity's cascade UUID
(`ANTIGRAVITY_CONVERSATION_ID`, used automatically when `--thread-id` is omitted)
are provider addresses, not the Meiva session handle.
For Codex, preserve `CODEX_HOME` and optionally supply `--codex-remote` so the
native relay reaches the owning server. For Antigravity, the request verifies
that the local `language_server_macos_*` ConnectRPC endpoint is reachable and the
accepting runtime starts the relay at activation. For Claude, start the `Monitor`
watch immediately after the request, armed with `timeout_ms: 1800000` and
re-armed at each expiry notice.
A dsh takeover runs from a DeepSeek Harness session that loaded the Meiva relay
plugin: the request writes the relay credential to the stub the plugin exports
(`DSH_MEIVA_RELAY_STUB`) instead of printing it, fails before creating a lease
when the plugin is absent, and the plugin starts the relay by itself.
See [host setup](agent-impersonation-hosts.md).

The response returns a per-agent integer `id` / `session_id`, starting at zero.
There is no controller credential: control commands are authorized by the
`(agent_id, session_id)` lease handle on the hosting machine. The claude and dsh
relays use the separate scoped `MEIVA_IMPERSONATION_RELAY_TOKEN`; that credential
belongs to the relay, never to the controller. Status/list responses omit
credentials.

```bash
meiva impersonate status 0 --agent 405
meiva impersonate list --agent 405 --limit 20
meiva impersonate list --agent 405 --before 20 --limit 20
```

Wait for `active`. `preparing` means native work or relay readiness is still
settling. `released`, `expired` and `rejected` are terminal. A fresh request
cannot overlap an open session or a pending native handoff. IDs never reset or
get reused. Names may repeat; the pair `(agent_id, session_id)` identifies work.

## Read context and act

```bash
meiva agents timeline 405 --limit 100 --before 23.0
meiva impersonate exec 0 --agent 405 --file operation.py
```

These reads authenticate with the local machine's cluster bearer and work
directly from an admitted bare executor on a gateway or pure runner. On a pure
runner, the lite CLI reads its installed API capability when no token is
inherited; an explicit `MEIVA_API_TOKEN` wins. A launcher descendant missing its
delivered token and a runner without an admitted local capability still have
no lite fallback. If an authorized lease attachment carries a bearer, it can
read the same window with `meiva impersonate exec 0 --agent 405`, then
`meiva.context.gateway.get("/api/agents/405/timeline")` with `params` as in the
CLI form (`{"limit": 100, "before": 23.0}`).

The exec form runs local Python in a short attachment. Direct Python uses:

```python
import meiva

with meiva.impersonation.attach(0, agent_id=405):
    meiva.agents.send_message(406, "Please review the login change")
```

Replies to human requests delivered through Meiva never go through the attachment — send them with the CLI (`meiva impersonate say`, see *Talk to the human*; peer messages have their own CLI form, see *Message another agent as the borrowed identity*).

Direct Python is the normal SDK path; `meiva impersonate exec` is an optional
wrapper. Use the cluster checkout's `.venv/bin/python` inside the controller's
own process tree, preserving its `MEIVA_HOME`. No long-lived interpreter is needed. Before work, run the impersonator skill's
bundled `scripts/read_instructions.py`
with the session id and `--agent`. Read its output in full: the system prompt and
configured preloaded skill notes saved in the current native context window,
not a newly rendered prompt. Restore relevant memory, tasks and workspace
context separately; follow the [impersonator guide](../../../meiva_builtins/skills/platform/impersonator-guide/SKILL.md)
for translating native execution instructions to your host.

An attachment binds the borrowed identity and saved configuration. SDK calls,
plugin state accesses and flush validate the active lease. Plugin changes are
journaled; the native graph applies them before resuming. Direct Python object
reads and native shell/editor actions are outside this cooperative guard.
Concurrent attachments fail on conflicting state versions. External deltas cannot
clear all message history. `meiva.self.restart`, `terminate` and `compact` remain
native-loop operations; include such needs in the release summary. Durable
`meiva.agents.restart` / `terminate` requests still reach the paused dispatcher.

## Message another agent as the borrowed identity

Sending to another Meiva agent as the played identity has a CLI form that runs
from the controller session's own process tree, like every control command:

```bash
meiva impersonate send 0 --agent 405 --to 406 --content 'Please review the login change'
```

The delivered source is `agent:405` — the borrowed identity, the same source the
attachment stamps for `meiva.agents.send_message` above. `--content -` reads the
message from stdin. Sending under an identity that is not one's own is exactly
what the lease attests — there is no declared form (task #4102).

## Talk to the human

Reply where each request entered. Direct human messages in the external host
conversation, including a manually opened Codex in tmux, get plain text there.
Human requests from Meiva get progress, questions and results through `say` below.
Meiva relay envelopes carry agent, lease and message ids plus each item's source;
the host message role alone cannot distinguish Meiva delivery from direct input.
Preserve the route for each request when messages interleave. Peer messages use
the borrowed peer channel above. Do not duplicate replies across entries unless
requested. Before investigating a human request, send a brief substantive reply
at its entry point; an immediate answer needs no separate acknowledgment.
Relay ACK and the final Meiva release summary remain separate requirements.

```bash
meiva impersonate say 0 --agent 405 --key progress-1 'I found the cause.'
meiva impersonate say 0 --agent 405 --key question-1 'Should the empty state offer sign-in?'
meiva impersonate say 0 --agent 405 --key final-1 --phase final 'The fix is verified.'
```

These replies appear immediately on the ordinary Meiva timeline with executor and
session metadata. Reuse a key only for retrying identical content. A repeated key
with different content fails. The logical sender is the Meiva agent; incoming user
messages retain their direction and sender. No frontend message table is split.

## Receive, ACK and renew

The relay pushes full inbound content, IDs and the exact ACK command. Acknowledge
each batch as soon as it arrives — receipt, not completion — then process it:

```bash
meiva impersonate ack 0 123 124 --agent 405
meiva impersonate inbox 0 --agent 405 --limit 100
meiva impersonate renew 0 --agent 405 --ttl 1800
```

Inbox reads do not ACK. Each lease snapshots the configured ACK window and
maximum total delivery attempts (defaults: 180 seconds, 2 attempts including
the first). Missing a final ACK window pauses automatic delivery of that message;
the lease remains active and inbox reads and late ACK remain available. See [delivery configuration](agent-impersonation-hosts.md#delivery-and-recovery).
An ACK records delivery receipt — never completion — and never affects history
retention. `cancel` asks the external controller
to stop; acknowledge it on receipt, then stop. `reminder` indicates an approaching TTL deadline:
acknowledge it on receipt, then decide whether to renew once or release. Renew explicitly, never in an automated
heartbeat loop. TTL is 1..86400 seconds; relay liveness does not extend it.

Messages are model work — a delivered message wakes a model on its receiving
side (tokens, not free): send substantive traffic (work, blockers, questions),
not chatter; routine status belongs in the release summary.

## Message states and delivery

An inbound row moves `pending` -> `claimed` -> `done` (or is dead-lettered,
a second deliberate arrival at `done` for a claim older than the stale
threshold). Native chat rows are confirmed at settlement points: every
finished turn, boot/recovery, and abort settlement. A claim whose message
never reached the checkpoint returns to `pending` and is delivered again —
a delivered message is never silently lost.

During an active takeover the native row stays `pending` even after the
executor's pipeline has it — that column is the native claim view, not a
delivery verdict. The web pending strip hides what the takeover has already
absorbed: while an unexpired active session exists, a pending chat that the
session trail has transcribed (what the timeline renders) or that the relay
has read appears in the timeline only — the same message is never shown
twice (#3683). If the session ends without an ACK, those rows are pending
and visible again for the native agent.

| kind | native flow | takeover flow |
| --- | --- | --- |
| chat | pending -> claimed -> done (a compact batch may fall a claim back to pending) | read -> ACK (receipt) -> done; unacknowledged, uncaptured rows stay pending for the native claim |
| cancel / terminate / restart | claimed -> done (control path; the host fulfils the intent) | delivered + ACK |
| restart_completed / resurrect / fork | first claim consumes it -> done | delivered + ACK |
| compact_request / compact_summary | claimed -> done | delivered + ACK |
| heartbeat | claimed -> done | not delivered during takeover; the native claim consumes it after |
| system_note / reminder | claimed -> done | delivered + ACK (a reminder also expires/dismisses) |

An automatic session's handoff receipt consumes its captured backlog to
`done`; uncaptured unacknowledged rows stay `pending` for the native agent. ACK
state is delivery bookkeeping, not completion: the resume note directs the
native agent to review acknowledged messages for work the summary does not
show as finished.

## Return control

Stop external work and close attachments before releasing:

```bash
meiva impersonate release 0 --agent 405 \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary must be written by the impersonator — naming any work left
unfinished, acknowledged or not. Meiva generates one JSON file at
`<agent workspace>/impersonation/0.json`, with session/process metadata, all
incoming/outgoing messages, inbound ACK state, lifecycle history, original
consumed SDK/API events and counts (calls, task changes, recipients, duration).
The file is saved before the native agent resumes. The summary and file path
arrive as the first new system note, ahead of subsequent normal input. Captured
unacknowledged input remains in the JSON for the native agent to handle, as does
acknowledged input whose work did not finish — an ACK records receipt, never
completion.

Expiry or failed activation also produces a handoff, explicitly stating the
reason and absence of an external summary. Session history is permanent,
including timestamps, renewals, ACKs and message bodies; exports can be rebuilt.
SDK statistics come from the existing event collector. This feature consumes
those facts and does not change collection or sampling.

An operator can end an open external session from the visible agent sidebar
row button or its context menu. The action closes the session shown in the
row, records `expired` with a `force-expired: ` reason and the caller in
permanent lifecycle history, and wakes the native agent. For a live
non-automatic session it also queues a
plain-language note that starts a resumed turn. The action does not stop the
external executor process. If the native runtime itself is wedged, terminating
the agent is the stronger fallback: the termination trigger revokes the session,
and the agent can then be resurrected. Its inbox records an impersonation
interruption note followed by a completed-termination note, ahead of the next
resurrection marker. The bound relay also reports that termination interrupted
the takeover through its usual host transport. Executor delivery is best-effort
when the relay or external session is unavailable; it never delays revocation.
