# No terminal UI (TUI) — the fleet console is the only supervision surface

## Context

Ava is an always-on, multi-agent fleet, not a single foreground session a user
watches in one terminal. Supervising it means seeing which agents exist, their
health, the spawn/fork/message graph between them, and task tracking — possibly
across several machines and many concurrently running agents at once. That is
inherently a rich, persistent, multi-focus surface, and the frontend
(`frontend.ava.okf.md`, a Next.js web console) already carries this
job. The question this record answers is whether Ava should *also* build a
terminal UI as a lighter-weight or CLI-native alternative — it should not, and
this is a deliberate design position, not a gap to fill later.

## Decision

Ava does not build a terminal UI. The product surface for fleet supervision is
the Next.js console — fleet monitoring, agent management, task tracking — plus
chat-style channels (e.g. Discord, installed via `ava mcp install`) for
talking to individual agents directly. A TUI is out of scope by design.

## Alternatives rejected

- **A curses/Ink-style TUI as a lighter alternative to the web console.**
  Rejected: an always-on fleet's state — N agents running concurrently, each
  with its own position in the spawn/fork graph, health, and task history —
  does not compress into a terminal's single-pane, single-focus model without
  dropping most of what supervision actually needs. A TUI good enough to
  really supervise a fleet ends up reinventing a windowing system inside a
  terminal emulator, which is strictly worse than the browser that already
  provides one.
- **A read-only terminal dashboard (e.g. a live `ava status`) as a stepping
  stone toward a fuller TUI.** Rejected as a *direction*, not as a feature:
  `ava status` / `ava cluster status` already exist and remain useful as
  one-shot ops/scripting commands (`conventions/runbook.md`). What's
  rejected specifically is escalating them into a persistent supervision
  surface meant to parallel or replace the web console.

## Consequences

- Fleet-supervision work goes into `frontend/` (Next.js) and the chat-channel
  integrations; no terminal rendering layer is built or maintained for it.
- `ava status` / `ava cluster status` are unaffected — they stay simple
  one-shot CLI commands for ops and scripting.
- Tracked as a standing non-goal in
  [`conventions/non-goals.md`](../conventions/non-goals.md). If Ava's
  usage model ever shifts from an always-on fleet to a single foreground CLI
  session per user, that describes a different product than the one this repo
  builds, and would need its own decision record rather than an amendment to
  this one.
