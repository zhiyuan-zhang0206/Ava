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

- **Start.** Your work begins with a start message from the delegating Ava
  agent, queued for the lease. It names the task and points at any context you
  need (workspace paths, checkouts, people to report to). Your first inbox read
  shows it; read it before acting.
- **End.** You end by releasing control with a summary. The release summary is
  your end message: what you did, what you verified, what remains open and
  where to resume from. Release wakes the Ava agent to continue.
- Everything between those two points happens under the lease. Nothing outside
  it — no acting after expiry, no self-restart, no fighting the lifecycle.

## Environment facts

Three values anchor every command in this skill:

- **Lease id** — appears in your activation hint (`Ava control active:
  agent=N lease=<uuid> ...`) and in the start message. Keep it handy.
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

Delivery is **push**, not poll. When inbound messages wait, a short hint lands
in your session — `Ava inbox ready: agent=N lease=<uuid> pending_page=...
newest_id=...`. The hint is only a wake-up: it carries no message bodies and is
not an acknowledgment.

On a hint (or right after activation, where the start message may still be
arriving), read the inbox once:

```bash
ava impersonate inbox <lease_id> --wait 30
```

`--wait` holds for new input (finite seconds); a fresh activation commonly
needs a short wait for the start message. Rows carry `id`, `content`, `kind`,
`source`, and `created_at`:

- `chat` — instructions and questions from the Ava agent or the user. The work.
- `system_note` — lifecycle information: the renewal reminder, expiry notice.
- `cancel` — stop your current work now (see below).

Process the rows you can handle, then acknowledge exactly those ids, promptly —
within about five minutes of reading:

```bash
ava impersonate ack <lease_id> 101 102
```

Rules that keep delivery honest:

- **Acknowledge only what you actually handled.** An unacknowledged message is
  treated as not processed: when its ACK window closes it is pushed again, and
  it remains pending for the Ava agent once control returns. Never ACK a
  message to silence delivery; if you cannot handle it, leave it
  unacknowledged and say so in your release summary.
- **Never poll, never write inbox code.** No watcher loops, no background
  inbox readers, no scheduled reads. The push side was built so you do not need
  any of that — react to hints when they arrive.
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

> The reminder delivery described below is part of the impersonation delivery
> rework (direct push + ACK window) and reaches the cluster together with that
> batch. If reminders do not yet arrive in your inbox, decide by the remaining
> TTL that `ava impersonate status` reports: renew once, when the remaining
> time no longer covers the work ahead — never on a schedule or in a loop.

The correct model:

1. Roughly **five minutes before the lease expires**, the Ava side delivers a
   renewal-reminder message into your inbox (you will see an inbox hint for
   it, like any other message).
2. On that reminder, decide: renew once, or start wrapping up.
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
  calls fail validation. Your unacknowledged messages and staged state are
  preserved; control returns to the Ava agent and you will see the expiry
  notice. Do not keep acting under the identity, and do not request a new
  lease on your own — a fresh takeover, if wanted, is arranged by the Ava
  side. Finish with an honest handoff instead: if you cannot release in time,
  the expiry notice itself reports the state, and anything still unacknowledged
  stays for the agent.

## Using the Python SDK under the lease

Ava's SDK is a Python namespace (`ava.*`). Under the lease you do not run the
Ava model — you attach your own Python process to the lease and call the SDK
directly with the borrowed identity.

Attach from the cluster's interpreter (the checkout's `.venv/bin/python`):

```python
import os
import ava

lease_id = "<lease_id>"          # from your activation hint / start message
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
