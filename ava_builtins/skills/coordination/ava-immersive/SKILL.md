---
name: ava-immersive
description: "Keeps a live human conversation continuously fed while heavy work runs in parallel. Use when the human starts a live session (immersive on, live pairing) — offer it when a live pass would help, but only the human can turn it on."
---

# Immersive Mode — keep the human fed while the work runs

A live-session discipline for a foreground agent whose human is present and
watching. The conversation stays continuously fed while heavy work runs in
parallel — on background shells, on other agents, or on a sleeping watcher.

## What this mode is for

- The human decides and steers; the agent carries the context and the pacing,
  so the human can drop in and out without losing the thread.
- Conversation pace and work duration are decoupled: replies follow the cap
  clock, work takes however long it takes in parallel.
- Without this discipline a live session degrades: the human waits (context
  goes cold, flow breaks), or does the work themself (the agent is bypassed).

## When it applies

- Human-triggered, explicitly. The human starts it ("immersive on", "let's do
  this live", accepting your offer). Never enter or extend a session on your
  own: where the human's attention goes is the human's choice, and an agent
  cannot predict it.
- You may offer a live pass once when it would genuinely help ("want to run
  this live together?"), then drop it.
- Scoped to the session: the live agents and the tasks the human is live on.
  Outside it, the default reporting discipline applies unchanged.

## The contract (announce on entry)

1. Silence cap — default 60 seconds; the human may set another value for the
   session or per task. State the value you are holding.
2. Inline vs delegate — what you keep inline (reads, searches, quick checks,
   conversation) and what goes out (anything that cannot finish inside one cap
   window).
3. Exit — the human ends the mode at any time; say what reverts when it ends.

## Rules

1. Silence cap. Never let more than the cap elapse with nothing said. If an
   action might overrun, split it, or send a pre-notice ("running X for ~2
   min; next update when it lands") and the result after.
2. Small steps inline, everything else out. Delegate to a worker
   (`ava.agents.spawn` / `send_message`), a background shell
   (`ava.shell.run_background`), or arm a watcher; be woken by completion
   instead of polling. Never block the conversation on long work.
3. Every message carries information: a finding, a progress delta, a decision
   needed, or the next step. Filler is banned; when nothing is new, say what
   you are waiting on and when the next signal comes.
4. Ledger in a file. Keep a durable ledger in your workspace: delegated tasks
   and owners, wait states, the human's decisions with a one-line rationale,
   and pointers to raw evidence. It survives compaction and restart, keeps the
   cadence honest, and is the handoff when the session ends.
5. Human first. A new human message preempts: finish at most the current small
   step, answer, then continue.
6. Protect the loop. Batch non-urgent updates arriving from other agents into
   the session rhythm; surface only what needs the human now. (Platform
   routing has no per-session do-not-disturb yet — do the filtering yourself.)
7. No fake busyness. This is a collaboration loop, not chatter: do not
   degrade work quality, rush judgment, or spawn redundant workers to look
   lively.

## Exiting

Close with: what changed, what is delegated and where the ledger is, what is
next. Then revert to the default discipline (queue results, no push,
milestone summaries). If the human falls silent mid-session, do not
manufacture updates: park work in the ledger, say what you are waiting on,
and wait.

## Relationship to the default reporting discipline

The default (`reduce-context-switch-for-human`) keeps the human unbothered
while away: results queue, updates batch, nothing depends on their attention.
Immersive mode is the other regime of the same attention surface, used while
the human is present. Both regimes keep heavy work parallel; they differ only
in when information reaches the human. Do not run immersive behavior outside
an active session, and do not leave a session nominally open once the human
is gone.
