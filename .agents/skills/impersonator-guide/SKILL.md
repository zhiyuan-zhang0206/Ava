---
name: impersonator-guide
description: 'Operating an Ava impersonation lease as the external agent: Ava CLI and Python SDK use under a borrowed identity, push-based message handling with prompt ACKs, reminder-driven lease renewal, and summary handoff. Use when an "Ava control active" hint names your lease or AVA_IMPERSONATION_TOKEN is set.'
---

# Acting as an Ava impersonator

An Ava agent approved your takeover: while the lease is active, you act as that
agent on this machine under a borrowed identity, and inbound messages to the
agent reach you. This skill is the complete operating manual for the lease —
how to use the Ava CLI and Python SDK, how messages flow, when to renew, and
how to end. It is self-contained: everything you need is here plus the values
in your start message.

## Operating contract

- **Start.** Your work begins with the start message: the Ava agent records a
  handoff brief when it approves the lease, and that brief is pushed to you
  first at activation. It names the task and points at any context you need
  (workspace paths, checkouts, people to report to). Read it before acting.
- **End.** You end by releasing control with a summary. The release summary is
  your end message: what you did, what you verified, what remains open and
  where to resume from. Release wakes the Ava agent to continue.
- Everything between those two points happens under the lease. Nothing outside
  it — no acting after expiry, no self-restart, no fighting the lifecycle.

## Environment facts

Three values anchor every command in this skill:

- **Lease id** — appears in the activation push and in the ACK command of
  every delivered batch. Keep it handy.
- **Token** — `AVA_IMPERSONATION_TOKEN` is set in your environment. Never print
  it, put it in a prompt, a command argument, a log, or a file.
- **The cluster executable** — use the `ava` CLI and Python interpreter of the
  cluster that hosts the agent (the checkout path was given to you, typically
  `<checkout>/.venv/bin/ava`). A bare `ava` on `PATH` can belong to a different
  cluster; the wrong executable cannot see this lease.

Check state any time:

```bash
ava impersonate status <lease_id>
```

The response shows the lease status and its expiry. Statuses you will see:
`requested` / `accepted` (not yet active), `active` (you may act), and terminal
`released`, `expired`, `rejected`.

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

After processing a batch, acknowledge exactly the ids you handled, within the
five-minute ACK window:

```bash
ava impersonate ack <lease_id> 101 102
```

(The envelope gives you the exact command, with the lease id filled in.)

Rules that keep delivery honest:

- **Acknowledge only what you actually handled.** A batch that is not
  acknowledged within the window is pushed again, explicitly marked as
  re-delivery, until it is ACKed or the lease ends; the ids make re-ACKing a
  batch you already handled harmless. Never ACK a message to silence delivery;
  if you cannot handle it, leave it unacknowledged and say so in your release
  summary.
- **Never poll, never write inbox code.** `ava impersonate inbox <lease_id>`
  remains only as a fallback read — for a missed or truncated push, or for a
  message's payload. The pushes are the delivery.
- **`cancel`**: stop the current work and ACK the cancel once stopped. To
  interrupt an in-flight tool immediately, use your own session's stop control;
  the ACK comes after the stop, not instead of it. An unacknowledged cancel
  stays pending for the Ava agent when control returns.

You can also send messages outward with the borrowed identity — questions to
the delegating agent, updates to peers — via the SDK below.

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
ava impersonate renew <lease_id> --ttl 3600
```

Pick the smallest TTL that covers the remaining work (1..86400 seconds; the
clock restarts at the moment you renew). Omitting `--ttl` keeps the current
length. After renewing, keep working — the next reminder comes before the new
expiry if the work is still running.

Hard rules:

- Renew **only in response to a renewal reminder**. No scheduled renewal, no
  "renew every hour just in case", no chained renewals without a fresh
  reminder, no background renewal process. If no reminder has arrived, you do
  not renew — the Ava side times reminders to the actual lease.
- If the lease expires while you work, stop immediately: further CLI and SDK
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

Attach from the cluster's interpreter (the checkout's `.venv/bin/python`):

```python
import os
import ava

lease_id = "<lease_id>"          # from the activation push / ACK commands
delegator_id = 405               # TODO: the Ava agent that delegated this work

with ava.external.attach(lease_id, token=os.environ["AVA_IMPERSONATION_TOKEN"]):
    print(ava.self.AGENT_ID)     # the borrowed agent id
    ava.agents.send_message(delegator_id, "Status: implementation done, verifying now")
    # any other ava.* SDK call that needs the agent identity
```

Inside the attachment the SDK resolves identity, plugins, and configuration as
the borrowed agent; peer messages and spawns carry that identity. The context
manager stages plugin state and flushes it on exit; for a long session call
`attachment.flush()` between steps — it never renews the lease.

For a one-shot operation, the CLI form runs a local Python file inside an
attachment without involving any Ava model:

```bash
ava impersonate exec <lease_id> --file operation.py
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
ava impersonate release <lease_id> \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary is required, nonempty, and concrete: state what you did, what you
verified, what remains open, and where to resume. It is queued as your final
message to the Ava agent, and release wakes the agent to continue from its
checkpoint. Release, not silence, is the ending: never leave an active lease
behind when you are finished.
