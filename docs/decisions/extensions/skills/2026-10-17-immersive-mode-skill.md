# Immersive mode ships as an opt-in, human-triggered skill

## Context

A human working live with an agent loses the session to turn latency: the
agent's natural reply cadence (seconds to minutes) leaves the human waiting,
context goes cold, or the human takes the work over. The default reporting
discipline is deliberately the opposite — queue results, do not push — and it
is right when the human is away. With dozens of agents, the human's scarce
resource is attention and context capacity; the fleet needs both a safe way to
stay silent and a smooth way to be present.

The boundary set for the mode on 2026-10-17: whether the human's attention
goes live is the human's decision. An agent cannot predict where the human
spends their time, so entry stays human-triggered — an agent may offer a live
pass, never start or extend one on its own.

## Decision

`ava-immersive` (built-in skill, `coordination/` group) carries the mode as an
instruction discipline first: human-triggered entry ("immersive on"); on entry
the agent announces the contract (silence cap default 60s, adjustable;
inline/delegate split; exit); it keeps a durable ledger (tasks, owners, wait
states, the human's decisions, evidence pointers), preempts for new human
messages, batches fleet pushes out of the live loop, and reverts to the
default discipline on exit. Heavy work always runs in parallel — the mode
changes only when information reaches the human.

The default reporting discipline is unchanged: nothing runs immersive
behavior outside an active, human-started session.

## Alternatives rejected

Platform routing first (a per-session do-not-disturb primitive): heavier,
slower to validate, and premature before there is evidence that instruction
discipline fails; the routing gap is recorded as an open item, and the
foreground agent filters relays itself for now. Enabling immersive behavior
by default (as a preset would): an agent cannot choose where the human's
attention goes, and turning live behavior on by default recreates the
interruption problem the default discipline exists to prevent. A long-lived
"front desk" role: not needed; the discipline attaches to whichever session
the human starts.

## Consequences

Live sessions become an explicit, contract-announced regime; agents outside
the session behave exactly as before. The skill's quality depends on
instruction compliance until routing support exists, and the first live
session (2026-10-17) provides the initial evidence. Open gaps: no per-session
do-not-disturb in message routing; v1 foreground scope is reads and light
interaction, with code, builds, and research delegated. Evaluation cases and
measures ship with the skill; the change stays unmerged until the live
session and requester review land.

Current guidance lives in [Immersive Mode](../../../../ava_builtins/skills/coordination/ava-immersive/SKILL.md).
