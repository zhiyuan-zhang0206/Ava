# 0001 — A rollout cannot deliver its own protection

**Date:** 2026-07-29 (recurred 2026-07-30)
**Anchors:** rollout `a87a23f5` → `05c58b35`, four-host cluster (macmini gateway,
macbook-air / win / wsl runners); recurrence on the 2026-07-30 21:09 rollout at
`7e571b4`. All three commits are pre-cutover — they exist in the archived
history, not in public `main`. Surviving code: `ops/controllers/code.py`,
`shared/cluster_lock.py:settle_update_lock`, `cli/commands/start.py:_readiness_waiver`.

## Summary

A deploy was planned twice as though a safeguard shipping *in that deploy* were
already protecting it. It was not: the first deployment of any safety mechanism
is, by construction, unprotected by that mechanism. Two hosts ended the rollout
frozen on old code with nothing able to heal them, and the deploy lease that was
assumed to be covering a manual pass had been released six minutes before the
pass began. The underlying reason is narrower and worse than "we forgot": the
orchestration process for any rollout is *always the old code*, because a running
Python interpreter does not adopt a tree checked out underneath it. New
orchestration behavior takes effect one rollout later than it lands. The rule now
lives in [`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md),
and the one mechanical guardrail is a design pattern rather than a check: a leg
that needs a new fact reads it itself instead of waiting to be told by its parent.

## Timeline

The rollout moved a four-host cluster from `a87a23f5` to `05c58b35`.

Two hosts — macbook-air and wsl — finished with their checkout on the new pin but
their processes still running the old commit: the `code ⚠` state, where `pin ✓`
and `code ⚠` are true at once. The plan assumed the watchdog's code controller
would notice and restart them. It did not. The controller ships *in* `05c58b35`
and is absent from `a87a23f5`; the watchdogs doing the noticing were the
pre-rollout processes, which had never heard of it. Confirmed directly with
`git cat-file -e <sha>:ops/controllers/code.py` against both commits. A human ran
`ava restart` on each host.

Later the same night, a manual Windows pass was carried out on the understanding
that the settle hold was keeping the deploy lease alive and auto-rollback
suppressed. `settle_update_lock` also ships in `05c58b35` — zero grep hits in
`a87a23f5`, three in `05c58b35`. The hold did not exist. The lease was observed
fully released six minutes before that pass began.

The next day the same shape produced an actual revert. The 21:09 rollout's local
gateway leg was spawned by an orchestrator that had imported `update.py` before
the checkout moved, so the parent could not pass a flag that the rollout itself
was introducing. The new child gated by default, the old parent read the child's
non-zero exit as a failed start and handed it to `_recover_rc`, and the `7e571b4`
orchestrator reverted a target whose gateway the watchdog had already revived and
had serving within sixty seconds.

## Root cause

A rollout executes under the process that started it. `ava cluster update`
(then `ava update --local`) begins, imports its orchestration modules, and only
afterwards moves the tree. From that moment the on-disk code and the executing
code are different programs, and every decision the rollout makes — what to spawn,
what flags to pass, when to give up, whether to roll back — is made by the older
of the two. The same holds for every daemon that was already running when the
checkout moved: watchdogs, restarters, controllers.

So a change to deploy safety is inert for exactly one rollout: its own.

The escape analysis is what makes this worth a file. Nothing was going to catch it:

- **Tests.** The suite exercises the orchestration as a single version. There is
  no fixture in which one commit's orchestrator drives another commit's tree,
  which is the only configuration where this bug is expressible at all.
- **Review.** The diff adding a controller reads as correct, because it is. The
  defect is not in the change; it is in the belief about when the change becomes
  live, and that belief lives in the deploy plan, not in the code.
- **The plan itself.** The rollout brief asserted the new safeguard as available.
  A brief is prose; nothing type-checks it.
- **The status surfaces.** `pin ✓` and `code ⚠` are separate dimensions precisely
  because a checkout can move without a restart — but reading them correctly still
  requires knowing that the healer for that state had not shipped yet.

Two independent occurrences in two days, once by the operator and once inside the
written brief, is what rules out carelessness and makes it a class.

## Guardrails added

- **Read the fact, do not wait to be handed it.**
  `cli/commands/start.py:_readiness_waiver` is the pattern that actually survives a
  version skew: instead of depending on a `--flag` the old parent would have to
  know to pass, the new child waives its readiness verdict when a gateway-capable
  host *observes* a live update lease (`status.py:_update_in_flight`). The same
  fact, reached entirely inside the new code. Scoped to gateway-capable hosts,
  because that is the only leg whose exit code a caller turns into a cluster-wide
  revert. Read the reasoning in place — it is the load-bearing comment on the
  function, and the same argument appears in
  [`cli/commands/start-readiness.ava.okf.md`](../cli/commands/start-readiness.ava.okf.md).
- **The code controller and the settle hold now exist**
  (`ops/controllers/code.py`, `shared/cluster_lock.py:settle_update_lock`). They
  protect every rollout after the one that shipped them, which was always the
  claim — it is only the rollout carrying them that they could not cover.
- **The rule, written down**: the `a rollout cannot deliver its own protection`
  entry in [`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).

**Still unguarded, deliberately.** Nothing mechanically detects "this plan assumes
a safeguard that ships in this rollout". The check is a human one and it is cheap:
`git cat-file -e <old-sha>:<path>`, or a grep of the old commit, before relying on
any mechanism during its own deploy.

## Lessons

- **The first deployment of any safety mechanism is unprotected by it.** Plan the
  manual and verification steps of such a rollout as if the old code were running,
  because it is. Assume the new mechanism is live from the *next* rollout.
- **A running process does not adopt a tree checked out underneath it.** This is
  the general form, and it reaches past deploys: any long-lived process — daemon,
  watchdog, orchestrator, agent — keeps executing the code it imported, and
  "the file on disk says otherwise" is not a rebuttal.
- **A flag cannot fix the rollout that introduces it.** When a leg needs a new
  fact, prefer a design where that leg reads the fact itself; a parameter has to
  be passed by a caller that, on the deciding rollout, predates the parameter.
- **Verify a mechanism's existence at the old commit, not at HEAD.** `git cat-file`
  against the sha you are deploying *from* is the whole check.
