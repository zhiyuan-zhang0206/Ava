# Import an existing agent's history into Ava (onboarding demo)

Roadmap item, unbuilt. Gated on going public — it only pays off when there are new
users to onboard.

## Why

A new user arriving from Claude Code / Codex already has months of accumulated
context: transcripts, `CLAUDE.md` / `AGENTS.md` conventions, per-project preferences,
hard-won operational facts. Today Ava starts them at zero and asks them to re-teach
all of it by hand — the worst possible first hour, and the point where most people
decide the migration isn't worth it.

A one-command import turns "set up a new agent" into "bring your agent over", which
is both a materially lower barrier and the most convincing thing a first-run demo can
show: *the system already knows your setup.*

## Shape

A **demo script**, not framework code — it should be an Ava agent doing a task with
the SDK it already has, so the whole thing reads as an example of what Ava is.

- Point it at the local history directory (`~/.claude/`, `~/.codex/`, …).
- It reads, distills, and writes into the memory pool: durable user preferences, per-
  project conventions, recurring commands/quirks. Distillation, not bulk copy — the
  memory pool must not become a transcript archive.
- It reports what it wrote so the user can review and correct before trusting it.

## Scope boundary

Not a general-purpose sync, not a live bridge, not a supported import format with a
compatibility contract. One direction, run once, at onboarding.

Prior art: a one-off migration of this shape has been done by hand before —
the same operation without the packaging. Whatever it learns about *what is
worth keeping* is the input to this script.
