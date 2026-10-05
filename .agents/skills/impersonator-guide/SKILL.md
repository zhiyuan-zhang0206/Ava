---
name: impersonator-guide
description: 'Operating an Ava impersonation lease as the external agent: Ava CLI and direct Python SDK use and inherited system-prompt guidance under a borrowed identity, push-based message handling with ACK-on-receipt, reminder-driven lease renewal, and summary handoff, with a host guide each for Claude Code, Codex and DeepSeek Harness. Use when an "Ava control active" hint names your lease or an impersonation session is active for your agent.'
---

# Acting as an Ava impersonator

A trusted takeover has reached active status: while the lease is active, you act as that
agent on this machine under a borrowed identity, and inbound messages to the
agent reach you. This skill is the complete operating manual for the lease —
how to use the Ava CLI and Python SDK, how messages flow, when to renew, and
how to end. Everything you need is in this guide, its SDK and host references, and your
briefing, which arrives inline in your launch message.

## Your host

The relay reaches each coding tool differently, so the host-specific half of
this manual — relay startup, how pushes arrive, where your authority comes
from, and each tool's traps — has one guide per host. Read yours before the
request:

- Claude Code: [reference/claude_code.md](reference/claude_code.md)
- Codex: [reference/codex.md](reference/codex.md)
- DeepSeek Harness (dsh): [reference/deepseek_harness.md](reference/deepseek_harness.md)

## Operating contract

- **Start.** Your briefing arrived inline in your launch message — read it
  before acting; it names the task and points at any context you need
  (workspace paths, checkouts, people to report to). Nothing about a takeover
  is file-based: no task file to read, no work file to write — but the borrowed
  agent's standing context is yours to recover: see *Before acting* below.
  Activation is an interruption, not a hand-merge — the Ava agent pauses at its
  checkpoint, and you and it never run at the same time.
- **Relay startup.** Your host's guide and your request output define it. A
  takeover activates only once its relay heartbeats; without one it is
  rejected, and the Ava agent keeps running.
- **End.** You end by releasing control with a summary. The release summary is
  your end message: what you did, what you verified, what remains open and
  where to resume from. Release resumes the Ava agent: one system note wakes it
  with your summary, and it continues from there. The Ava side also stops a
  lease when your executor is confirmed dead or its original TTL expires. Relay
  failure degrades delivery; the lease-scoped end note names the cause. A stopped lease
  is over: do not keep acting, and do not request a new takeover — a fresh one
  is arranged by the Ava side.
- Everything between those two points happens under the lease. Nothing outside
  it — no acting after expiry, no self-restart, no fighting the lifecycle.

## Before acting: inherit the borrowed context

You are not a new agent picking up a fresh task — you are the continuation of
the agent whose identity you hold, and the briefing is a quick entry point, not
the whole context. Before starting the work, recover the standing context it needs:

- **Standing instructions first.** Read `attachment.instructions()` through the
  direct SDK attachment below. It returns the system prompt and configured
  preloaded skill bodies active in the borrowed agent's current conversation,
  including core behavior, plugin rules, role guidance and the capability
  index. It preserves what the agent actually read, rather than regenerating
  guidance from newer configuration. If it fails, resolve the missing context
  with the Ava side before starting work; never substitute a generic prompt.
- **Skills and configuration.** Follow the inherited capability index and load
  skills relevant to this work with `ava.help(ava.skills.<identifier>)`; read
  their referenced files as needed. The attachment uses the borrowed agent's
  saved configuration for SDK calls. Read any additional role/config files
  named by the briefing or inherited instructions.
- **Memory.** The `memory/MEMORY.md` index in its workspace (default
  `~/.ava/workspaces/<agent_id>/`) and the entries this work touches.
- **Shared rulings and facts.** The shared memory pool (`ava.memory`) — the
  standing rules and user rulings other agents already work by.
- **Work in progress.** Its open tasks (`ava.tasks`) and any handoff or progress
  notes left in its workspace.
- **Role and boundaries.** What this identity owns and how it collaborates — its
  label, the role notes around it, and the conventions others hold it to.

Read the standing instructions in full once, then recover other context
selectively, just in time — what the work in front of you needs, not everything
at once. Treat memory as facts and user decisions, not as authority to silently
replace the inherited core/plugin operating rules. If they conflict, follow
explicit applicable user decisions and surface unresolved conflicts to the Ava
side instead of inventing a new policy.

The borrowed instructions describe native Ava execution. Keep their role,
behavior, collaboration and verification rules; translate native
`execute_code` operations into direct Python SDK calls under the attachment.
Use your host's own tools for local work and this guide's lease controls for
receipt, renewal, user replies and release. The borrowed model identity is not
your executor's model identity, and native-loop lifecycle actions remain the
Ava agent's responsibility.

Under the SDK attachment, `ava.cwd` and `ava.files` resolve
against the borrowed agent's workspace; outside it, read the files directly on
this machine. Never substitute new-agent assumptions for the borrowed context: a
takeover that invents a process or a gate the agent never had — or ignores one it
did — fights its own owner; if something you need is not exposed to you, read it
on this machine instead of guessing.

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
- **The cluster executable** — run the commands as given, with a bare `ava`. It acts
  on the home `AVA_HOME` names, else `~/.ava`: launched by an Ava agent, you
  inherited that agent's `AVA_HOME`; in a terminal of your own on the hosting
  machine, leave it unset. An `ava` from a checkout that is not the home's own
  refuses every command rather than guess.

Check state any time:

```bash
ava impersonate status <session_id> --agent <agent_id>
```

The response shows the lease status and its expiry. Statuses you will see:
`preparing` (not yet active), `active` (you may act), and terminal
`released`, `expired`, `rejected` (`expired` also covers a takeover the Ava
side stopped after confirmed executor death — the session record names the
cause).

## Messages: receive, acknowledge, process

Delivery is **push**, not poll. The bound relay delivers every inbound batch
to your session as one self-contained envelope: the full message content, the
message ids, and the exact ACK command to run. There is no inbox code to write
and nothing to poll — acknowledge a batch as soon as it arrives, then do the
work it asks for; the ACK confirms receipt, not completion.

Messages carry a `kind` that tells you how to treat them:

- `chat` — instructions and questions from the Ava agent or the user. The work.
- `reminder` — a lease-expiry renewal reminder from Ava, pushed about five
  minutes before the TTL elapses (see Renewal below).
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

The correct model:

1. Roughly **five minutes before the lease expires**, the Ava side delivers a
   renewal reminder — a `reminder` message pushed through the same envelope
   path as everything else.
2. Acknowledge the reminder as soon as it arrives — receipt, like any other
   message — then decide: renew once, or start wrapping up.
3. To renew, extend from now for the time you still need:

```bash
ava impersonate renew <session_id> --agent <agent_id> --ttl 1800
```

The reminder's own renew command repeats your current window as a starting
point; set `--ttl` to what the remaining work needs.

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
  takeover after confirmed executor death — stop immediately: further CLI and SDK
  calls fail validation. Control returns to the Ava agent with your
  unacknowledged messages and staged state preserved. Do not keep acting
  under the identity, and do not request a new lease on your own — a fresh
  takeover, if wanted, is arranged by the Ava side. Hand back honestly instead:
  release with a summary whenever you still can, naming every unfinished piece,
  acknowledged or not; if expiry catches you, anything unacknowledged stays for the agent.

## Using the Python SDK under the lease

Use direct Python as the normal SDK path: `ava.external.attach` binds the borrowed
execution context without a CLI prefix. First read `attachment.instructions()`;
then call `ava.*` from short Python invocations under the same lease. Close each
attachment before releasing control.

Read [reference/sdk.md](reference/sdk.md) before your first attachment for the
interpreter, complete examples, optional CLI wrapper and lifecycle boundaries.

## Finishing: release with a summary

When the work is done — or when you must stop before it is — close attachments,
acknowledge any batch you received but have not yet acknowledged, and release:

```bash
ava impersonate release <session_id> --agent <agent_id> \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary is required, nonempty, and concrete: state what you did, what you
verified, what remains open — including work whose input you acknowledged but
did not finish; an ACK means received, never done — and where to resume. Ava writes one JSON file at
`<agent workspace>/impersonation/<session_id>.json`, containing every incoming
and outgoing message, ACK state, lifecycle history, consumed SDK/API events and
statistics. Your summary plus that file path is the first new system note in
the resumed agent's input. History is permanent. The resumed agent must review the file's incoming
messages — including the ones you acknowledged, since an ACK records receipt,
not completion — and finish what remains. Expiry has no invented summary. Release, not silence, is the ending: never leave an active lease
behind when you are finished.
