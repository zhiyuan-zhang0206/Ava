---
name: impersonator-guide
description: 'Operating an Ava impersonation lease as the external agent: Ava CLI and Python SDK use under a borrowed identity, push-based message handling with prompt ACKs, reminder-driven lease renewal, and summary handoff. Use when an "Ava control active" hint names your lease or an impersonation session is active for your agent.'
---

# Acting as an Ava impersonator

A trusted takeover has reached active status: while the lease is active, you act as that
agent on this machine under a borrowed identity, and inbound messages to the
agent reach you. This skill is the complete operating manual for the lease —
how to use the Ava CLI and Python SDK, how messages flow, when to renew, and
how to end. It is self-contained: everything you need is here plus your
briefing, which arrives inline in your launch message.

## Operating contract

- **Start.** Your briefing arrived inline in your launch message — read it
  before acting; it names the task and points at any context you need
  (workspace paths, checkouts, people to report to). Nothing about a takeover
  is file-based: no task file to read, no work file to write. Activation is an
  interruption, not a hand-merge — the Ava agent pauses at its checkpoint, and
  you and it never run at the same time.
- **End.** You end by releasing control with a summary. The release summary is
  your end message: what you did, what you verified, what remains open and
  where to resume from. Release resumes the Ava agent: one system note wakes it
  with your summary, and it continues from there. The Ava side also stops a
  lease by itself when a core component dies — your executor process, or the
  relay delivering your messages; the end note names the cause. A stopped lease
  is over: do not keep acting, and do not request a new takeover — a fresh one
  is arranged by the Ava side.
- Everything between those two points happens under the lease. Nothing outside
  it — no acting after expiry, no self-restart, no fighting the lifecycle.

## Environment facts

Three values anchor every command in this skill:

- **Agent id and session id** — appears in the activation push and in the ACK command of
  every delivered batch. The session id is an integer scoped to its Ava agent; keep both handy.
- **Session authority** — no credential exists to hold or pass. Every control
  command must run from inside this session's own process tree (the executor and
  its children); the session id plus that presence is the authority. A command
  from any other process tree is refused — never move control to a helper. Run control
  commands directly in the executor session's own shell: the caller's ancestor chain must
  contain the recorded executor anchor within 8 levels — deeper wrapping fails closed.
- **The cluster executable** — use the `ava` CLI and Python interpreter of the
  cluster that hosts the agent (the checkout path was given to you, typically
  `<checkout>/.venv/bin/ava`). A bare `ava` on `PATH` can belong to a different
  cluster; the wrong executable cannot see this lease.

Check state any time:

```bash
ava impersonate status <session_id> --agent <agent_id>
```

The response shows the lease status and its expiry. Statuses you will see:
`preparing` (not yet active), `active` (you may act), and terminal
`released`, `expired`, `rejected` (`expired` also covers a takeover the Ava
side stopped because a core component died — the session record names the
cause).

## Messages: receive, process, acknowledge

Delivery is **push**, not poll. The bound relay delivers every inbound batch
to your session as one self-contained envelope: the full message content, the
message ids, and the exact ACK command to run. There is no inbox code to write
and nothing to poll — process what arrives, then acknowledge it.

Messages carry a `kind` that tells you how to treat them:

- `chat` — instructions and questions from the Ava agent or the user. The work.
- `reminder` — a lease-expiry renewal reminder from Ava, pushed about five
  minutes before the TTL elapses (see Renewal below).
- `cancel` — stop your current work now (see below).
- `system_note` — platform lifecycle information (rare during a lease).

After processing a batch, acknowledge exactly the ids you handled, within the
five-minute ACK window:

```bash
ava impersonate ack <session_id> 101 102 --agent <agent_id>
```

(The envelope gives you the exact command, with the lease id filled in.)

Rules that keep delivery honest:

- **Acknowledge only what you actually handled.** A batch that is not
  acknowledged within five minutes is pushed once more, explicitly marked as
  the final delivery. Another five minutes without ACK ends impersonation and
  returns unacknowledged input to the native agent. The two-attempt budget is
  per message and survives relay restarts; the ids make re-ACKing a batch you
  already handled harmless. Never ACK a message to silence delivery;
  if you cannot handle it, leave it unacknowledged and say so in your release
  summary.
- **Never poll, never write inbox code.** `ava impersonate inbox <session_id> --agent <agent_id>`
  remains only as a fallback read — for a missed or truncated push, or for a
  message's payload. The pushes are the delivery.
- **Confirm the start message.** Activation, relay liveness, and transport
  acceptance are not host receipt: confirm the start message actually arrived
  in your conversation before relying on pushes. If it did not, use the
  fallback read and note the miss in your release summary.
- **`cancel`**: stop the current work and ACK the cancel once stopped. To
  interrupt an in-flight tool immediately, use your own session's stop control;
  the ACK comes after the stop, not instead of it. An unacknowledged cancel
  stays pending for the Ava agent when control returns.

Send user-facing progress, questions and results to the normal Ava UI:

```bash
ava impersonate say <session_id> --agent <agent_id> --key progress-1 'Checking the fix.'
```

Choose a new stable key for each message; retry with the same key and identical
content after ambiguous delivery. Use `--phase final` for a final reply.
`--as` is your freely chosen display name and the session has its own `--name`;
both are set at request time and neither is a `say` parameter.
The UI shows no executor or session badge on your messages — they render on the
normal timeline as the Ava agent's own, with both values recorded in the session
metadata. The CLI records observed process facts
separately. For peers, use the borrowed identity through the SDK below, or from the CLI with `ava impersonate send`.

**Message economy.** Send only what the work needs: work content, blockers,
questions. Every message — and every hint, re-delivery or reminder it
triggers — is model work somewhere (tokens, not free). No pleasantries, no
courtesy pings, no duplicate notices of one fact, no "just checking in". One
substantive message per milestone beats a stream of small ones.

## Renewal: only when the Ava side reminds you

The lease TTL is the recovery boundary: if you die or the connection breaks,
control must come back to the Ava agent when the lease expires. That is why
renewal is an explicit, human-scale action and never an automated loop. A
background renewer once kept a dead session's identity alive for hours —
renewing every hour for a 24-hour TTL — until control was lost and the agent
hung. Do not recreate that failure mode.

> If renewal reminders do not yet arrive in your inbox (reminder delivery
> ships with the impersonation push-delivery batch), decide by the remaining
> TTL that `ava impersonate status` reports: renew once, when the remaining
> time no longer covers the work ahead — never on a schedule or in a loop.

The correct model:

1. Roughly **five minutes before the lease expires**, the Ava side delivers a
   renewal reminder — a `reminder` message pushed through the same envelope
   path as everything else.
2. On that reminder, decide: renew once, or start wrapping up. Then ACK the
   reminder like any other message.
3. To renew, extend from now for the time you still need:

```bash
ava impersonate renew <session_id> --agent <agent_id> --ttl 3600
```

Estimate the TTL short — pick the smallest window that covers the work ahead
(1..86400 seconds; the clock restarts at the moment you renew). The same rule
applies when a lease is requested up front: for a task that looks like about
an hour, ask for about 30 minutes and extend in steps — several short renewals
are the intended pattern, not a failure. Each renewal is a deliberate liveness
check, and a short window is the backstop that returns control to the Ava
agent soon after the session dies instead of parking the agent for a long
span. `--ttl` is required: state the length you need outright. After renewing, keep working
— the next reminder comes before the new expiry if the work is still running.

Hard rules:

- Renew **only in response to a renewal reminder**. No scheduled renewal, no
  "renew every hour just in case", no chained renewals without a fresh
  reminder, no background renewal process. If no reminder has arrived, you do
  not renew — the Ava side times reminders to the actual lease.
- If the lease ends while you work — TTL expiry, or the Ava side stopping a
  takeover whose core component died — stop immediately: further CLI and SDK
  calls fail validation. Control returns to the Ava agent with your
  unacknowledged messages and staged state preserved. Do not keep acting
  under the identity, and do not request a new lease on your own — a fresh
  takeover, if wanted, is arranged by the Ava side. Hand back honestly
  instead: release with a summary whenever you still can; if expiry catches
  you, anything still unacknowledged stays for the agent.

## Using the Python SDK under the lease

Ava's SDK is a Python namespace (`ava.*`). Under the lease you do not run the
Ava model — you attach your own Python process to the lease and call the SDK
directly with the borrowed identity.

User-visible replies never go through the attachment — send them with the CLI (`ava impersonate say <session_id> --agent <agent_id> --key <key> 'text'`; see the message-handling section above).

Attach from the cluster's interpreter (the checkout's `.venv/bin/python`):

```python
import ava

session_id = 0                 # from the activation push
agent_id = 405                 # the Ava agent you are replacing

with ava.external.attach(session_id, agent_id=agent_id):
    # Call other ava.* capabilities under the borrowed identity.
```

Inside the attachment the SDK resolves identity, plugins, and configuration as
the borrowed agent; peer messages and spawns carry that identity. The context
manager stages plugin state and flushes it on exit; for a long session call
`attachment.flush()` between steps — it never renews the lease.

Messaging another Ava agent needs no attachment — the CLI carries the borrowed
identity, attested like every control command:

```bash
ava impersonate send <session_id> --agent <agent_id> --to <target_agent_id> --content 'Status: X done, Y open.'
```

The delivered source is `agent:<agent_id>`, exactly what `ava.agents.send_message`
stamps inside the attachment.

For a one-shot operation, the CLI form runs a local Python file inside an
attachment without involving any Ava model:

```bash
ava impersonate exec <session_id> --agent <agent_id> --file operation.py
```

Omit `--file` to read the program from stdin.

Boundaries: `ava.self.compact`, `ava.self.terminate`, and `ava.self.restart`
end the *native* agent's execution loop — they are not yours to call. If the
work concludes the agent should compact or reconfigure, say so in the release
summary. Durable lifecycle requests for the agent (`ava.agents.restart`,
`ava.agents.terminate`) reach the native dispatcher while it is parked, but
treat them as last resorts: flush pending plugin state first, and prefer
leaving lifecycle decisions to the Ava side.

## Finishing: release with a summary

When the work is done — or when you must stop before it is — close your
attachments, ACK the messages you handled, and release:

```bash
ava impersonate release <session_id> --agent <agent_id> \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary is required, nonempty, and concrete: state what you did, what you
verified, what remains open, and where to resume. Ava writes one JSON file at
`<agent workspace>/impersonation/<session_id>.json`, containing every incoming
and outgoing message, ACK state, lifecycle history, consumed SDK/API events and
statistics. Your summary plus that file path is the first new system note in
the resumed agent's input. History is permanent. The resumed agent must read
unacknowledged incoming messages in the file. Expiry has no invented summary. Release, not silence, is the ending: never leave an active lease
behind when you are finished.
