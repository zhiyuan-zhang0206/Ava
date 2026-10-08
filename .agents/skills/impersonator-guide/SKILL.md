---
name: impersonator-guide
description: "Operates an active Ava impersonation lease as an external agent. Use when a takeover launch or lease hint supplies a borrowed Ava identity."
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

- **Standing instructions first.** Run [scripts/read_instructions.py](scripts/read_instructions.py)
  as described in [reference/sdk.md](reference/sdk.md). It prints the system
  prompt and configured preloaded skill bodies active in the borrowed agent's current conversation,
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

Delivery is push-based. Read [message receipt](reference/message-receipt.md)
before handling a delivered batch: receive its full body, ACK receipt promptly,
then process it and reply at the request's entry point. ACK never means completion.
Stop cancelled work; do not poll for ordinary delivery.

## Renewal: only when the Ava side reminds you

Renew only after an Ava renewal reminder, as a deliberate liveness decision.
Read [lease renewal](reference/lease-renewal.md) when that reminder arrives.
Never run a background renewer. Stop acting immediately when the lease ends.

## Using the Python SDK under the lease

Use direct Python as the normal SDK path: `ava.external.attach` binds the borrowed
execution context without a CLI prefix. First run the bundled instruction-reader
script; then call `ava.*` from short Python invocations under the same lease. Close each
attachment before releasing control.

Read [reference/sdk.md](reference/sdk.md) before your first attachment for the
interpreter, complete examples, optional CLI wrapper and lifecycle boundaries.

## Finishing: release with a summary

When done or stopping early, close attachments and read
[release](reference/release.md). Release with a concrete summary of verified
work and unfinished input; never leave an active lease behind.
