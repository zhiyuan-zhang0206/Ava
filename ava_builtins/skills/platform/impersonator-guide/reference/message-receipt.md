# Lease message receipt

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Messages: receive, acknowledge, process

Delivery is **push**, not poll. The bound relay delivers every inbound batch
to your session as one self-contained envelope: the full message content, the
message ids, and the exact ACK command to run. There is no inbox code to write
and nothing to poll — acknowledge a batch as soon as it arrives, then do the
work it asks for; the ACK confirms receipt, not completion.

Messages carry a `kind` that tells you how to treat them:

- `chat` — instructions and questions from the Ava agent or the user. The work.
- `reminder` — a lease-expiry renewal reminder from Ava (see Renewal below).
- `heartbeat` — a periodic check-in under the borrowed agent's heartbeat rules.
  Acknowledge it and continue useful work, or call `ava.self.pause_heartbeat(seconds)`
  under the SDK attachment for a known wait or uninterrupted work period. The
  clock runs throughout an active lease, regardless of external activity; the
  pause survives native return; it does not pause expiry reminders or renew the lease.
- `cancel` — stop your current work now (see below).
- `system_note` — platform lifecycle information (rare during a lease).

Acknowledge each batch as soon as it arrives — receipt is the ACK — within the
ACK window stated in the envelope (180 seconds by default):

```bash
ava impersonate ack <session_id> 101 102 --agent <agent_id>
```

(The envelope gives you the exact command, with the lease id filled in.)

Rules that keep delivery honest:

- **Acknowledge what you have actually received.** An ACK records receipt;
  finishing work goes through say and the release summary, never an ACK. A batch
  not acknowledged within its ACK window is retried up to the lease's configured
  total attempt limit (default: 2 attempts, each with 180 seconds to ACK);
  missing the final window pauses automatic delivery of that message; read and
  ACK it through the inbox. The budget is fixed at request time and survives
  relay restarts; the ids make re-ACKing a batch you already received harmless.
  A message you cannot act on is still acknowledged on receipt — say what you
  could not do in your release summary.
- **Never poll, never write inbox code.** `ava impersonate inbox <session_id> --agent <agent_id>`
  is a fallback read only — for a missed or truncated push, or for a message's
  payload; the pushes are the delivery. A truncated push is not received yet: fetch the
  full body with the inbox command first (the fetch is part of receiving), then
  ACK — once acknowledged, the inbox no longer returns the row; acknowledged
  before reading? The body is not lost: on an admitted runner or gateway read it
  with `ava agents timeline <agent_id>`; otherwise read it inside the lease attachment —
  `ava impersonate exec`, then `ava.context.gateway.get("/api/agents/<agent_id>/timeline")`.
- **Confirm the start message.** Activation, relay liveness, and transport
  acceptance are not host receipt: confirm the start message actually arrived
  in your conversation before relying on pushes. If it did not, use the
  fallback read and note the miss in your release summary.
- **`cancel`**: acknowledge it on receipt like every message — receipt is the
  ACK — then stop the current work now; for an in-flight tool use your own
  session's stop control — the ACK never substitutes for stopping. An
  unacknowledged cancel stays pending for the Ava agent when control returns.

**Reply at the request's entry point.** A direct human message in your host
conversation (including a Codex opened manually in tmux) gets plain text there.
A human request delivered through Ava gets progress, questions and results via
`ava impersonate say` in Ava. Identify Ava delivery by its envelope header
(`Ava message agent=... lease=... ids=...`) and item `from=` source:
`kind=chat from=user` is a human request; `from=agent:N` uses the peer channel below. The host's user role alone does
not identify the entry point: the relay also delivers into that role.
Keep the reply route with each request when host and Ava messages interleave;
do not redirect an earlier request's result to the newest message's entry.
Before investigating a human request, give a brief substantive reply at that
entry; a direct answer needs no separate acknowledgment. Relay ACK is separate
and still required. Do not duplicate replies across entries unless requested.
Host text does not replace the required Ava release summary.

For Ava replies:

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
