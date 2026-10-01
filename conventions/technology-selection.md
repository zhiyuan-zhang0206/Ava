# Technology selection

How to choose a mechanism, library or architecture here. "Don't reinvent" (core principle 3) says
what to avoid; this page says in what order to look, when a human decides, and what to write down.
It is a design-time tool: it orders the options, it does not replace judgment.

## Three kinds of decision, three postures

| Kind | Examples | Posture |
|---|---|---|
| **Solved general problem** | concurrency, durability, retries, time, scheduling, crypto, parsing, idempotency, backups | Use the mature answer. The cost of these problems is in their **failure modes** (half-done work, reordering, duplicates), so a hand-rolled version almost always misses some. |
| **Scale and decomposition** | one process or many, how many services, sharding | Start with the simplest design and change it when a measured limit is reached. Name the limit and the assumption up front (see Lifetime). |
| **Hard to reverse** | persisted formats, wire protocols, database schemas, process boundaries, public interfaces | Think one step further, even with a single user: migrating these later is the expensive part. Prefer formats others can read. |

"Brute force" means a **simple design**, not a hand-written implementation of something already solved.

## The ladder

Stop at the first rung that works.

**0. Frame the problem.** Restate it in its most standard form (a periodic trigger, store-and-forward,
point-in-time recovery, a supervisor), at the outermost level where prior art exists, and say why
any deviation is needed. A hard constraint justifies only the smallest piece it forces; everything
around it still goes through the ladder.

**1. Do nothing, or use the standard library or the platform.** If the shared store itself may be
the thing that is down (a sender whose message cannot reach the gateway), the answer is a local
standard-library store (for example `sqlite3`), not a custom file format.

**2. Use what the stack already has** (Postgres, Redis, LangGraph, psycopg). Two checks: *coverage* —
can the mechanism reach every call site, and outside its scope does it raise or silently pass? —
and *storage semantics* — does the store's ordering, cursor, integrity and retention fit the
consumer? An observability store (logs, Loki) is not a system of record.

**3. A mature, boring library.** Widely used, maintained, few dependencies, no framework lock-in,
behind a thin adapter. Price our own code against the dependency: a library that forces us to
rebuild the model it assumes saves little. Persisted artifacts must stay readable without us.

**4. Write it ourselves** only when the problem is small, our requirement truly differs from what
libraries assume, and the failure modes are understood. Write down, in the same change:
the condition under which it gets deleted, the stop-loss (size or time past which we switch to a
mature tool), and that a second user of the same hand-written pattern triggers extraction or a
move up a rung.

## Lifetime checks (before accepting any rung)

- **What happens to its state on restart and on a crash mid-operation?** Volatile state is acceptable
  only when losing it is harmless (idempotent or compare-and-set).
- **Who calls it today?** A guard or mechanism outlives the capability it protected; re-ask when a
  consumer goes away.
- **What assumption does the simple design rely on, and what is the signal that it no longer
  holds?** Write it down and make it checkable (a lint, a test, a metric) instead of leaving it in a
  comment. A change of scale axis (one process per agent to one process serving many) silently
  invalidates a batch of assumptions; produce the list when it happens.
- **Revisit the claimed benefit** once after it ships.

## Gates: a human decides

Crossing any of these needs sign-off from the owner, recorded in the PR description or, for lasting
choices, in `decisions/`: a new dependency or an upgrade; a new process or service (a sleeping
resident process counts); a new datastore, persisted format or protocol version; a new global
singleton or shared mutable state; a new background process. Inside the ladder, choices need no
sign-off, only a stated reason.

## The record: five questions

Every proposal that crosses a gate answers, in a few lines each:

1. **Boundary** — what the mechanism owns and what it does not.
2. **Prior art** — how the field usually solves it, and which rung that is.
3. **Simplest option** that works.
4. **Failure prevented** — the concrete failure, and the failure modes we must now handle ourselves.
5. **Limit signal and exit** — what measurement makes us change course, and where we go.

## Keep or replace existing code

The ladder tells you where to start; it does not say existing code must move up. Replace when
either the format or gate is one-way and private (only we can read it), or the carrying cost of our
own code (lines, fix rate, knobs) times the horizon exceeds the migration cost *and* it hurts now.
Otherwise keep it and write down the condition that would make you revisit.

## Where to look when unsure

Ask: will anything read this to decide what to do? Is it a solved problem? Is this change easy to
undo? If you cannot answer, that is a gate: ask.
