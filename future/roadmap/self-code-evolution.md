# Autonomous self-code-evolution (north star — generation 2)

The largest ambition. Not urgent, but it is the thing the rest of the
architecture is quietly building toward, so it must never fall off the map.

This is **generation 2** of one machine: `observe -> propose -> guardrail ->
promote`, here with the target = *code* and the guardrail = container-eval + CI
+ PR. Generation 1 ([`autonomous-learning-loop.md`](autonomous-learning-loop.md))
runs the same machine over the *text* target (skill + memory) — it ships first
because text is git-reversible and needs no sandbox. Read the two together.

## The bet

Competitors that ship a "learning loop" (Hermes' Curator, OpenClaw's Skill
Workshop) evolve only **skills** — text the agent reads. Ava does that too, as
gen 1 — but the north star is one level deeper: the agent evolves its **own
codebase** — the framework, the SDK, the kernel — autonomously.

The reason this is tractable for Ava specifically is the CodeAct architecture.
Because the agent already acts by composing Python over the `ava.*` namespace,
and because the prod-upgrade path is already first-class
(`ava cluster update` → cluster-wide PR-merged rollout; the one-time SDK
call `ava.self.update()` was removed 2026-08), "modify yourself" is not
a new mechanism — it is the agent driving the loop it already has:

> read the codebase → write a change → open a PR → CI → merge → `ava cluster update`
> → the whole cluster restarts on the new code.

Today a human drives that loop. The north star is the agent driving it, end to
end, against a goal — with the same fail-fast, PR-gated discipline a human
follows (never edit-self-and-reload; always PR → CI → merge → update).

## What it is NOT

- **Not** in-process self-patching / hot-reload of running code. The change goes
  through the real PR → CI → merge → rollout path; CI is the safety gate.
- **Not** skill/memory-text self-improvement — that is gen 1
  ([`autonomous-learning-loop.md`](autonomous-learning-loop.md)), which ships
  first and unblocked. This doc is about the *code*, the unsolved, higher-value
  half that the sandbox + eval fuse gates.

## Why it is gated (and on what)

Two hard prerequisites, both already on the roadmap as their own items:

1. **A real exec sandbox** ([`docker-sandbox.md`](docker-sandbox.md)). An agent
   that can rewrite its own framework while running unconfined on the host is the
   maximal blast radius. Self-code-evolution does not start until the exec
   boundary exists.
2. **An eval harness** (SWE-bench / GAIA, per [`non-goals.md`](../../conventions/non-goals.md)). An
   autonomous code-change loop with no objective scorecard optimizes for nothing
   measurable and can silently regress. The loop must be able to grade its own
   change before merging. No eval, no go.

This is the same fuse the Permissions and "self_evolution" non-goals already
name — they are one gated cluster behind the sandbox, not independent noes.

## Open design questions (for when it is picked up)

- The merge gate: fully autonomous merge vs. human-approval-in-the-loop for the
  first generations. Almost certainly human-gated first, autonomous later.
- Goal/scorecard source: which eval the loop optimizes against, and how a
  regression on the eval blocks the merge.
- Blast-radius containment beyond CI: can a bad self-change brick the cluster's
  ability to run the *next* self-change (i.e. is the rollout reversible enough —
  this leans on the paired down-migrations + `rollback_to` work already in
  `future/infra/commit-pinned-cluster.md`).
