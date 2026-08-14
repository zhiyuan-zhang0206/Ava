# Cluster consistency: commit-level pinning (vs schema-level)

> **Status: increments A and B are both built. What remains — hard fail-fast
> enforcement — has been overtaken by a later decision and needs re-litigating
> before it is built.**
>
> - **Increment A (persist + visualize) — done.** `cluster_target_sha` is a standing
>   value in the `cluster_pin` table (`shared/cluster_pin.py`); the gateway writes it
>   after each rollout and `ava status` surfaces per-node drift from it.
> - **Increment B (health-probe + rollback) — done.** `last_known_good_sha`,
>   `ava cluster health-probe`, `ava cluster rollback --to <tag|sha>`, and OS cron
>   registration all shipped; see
>   [`../../decisions/2026-06-29-self-evolution-rollback.md`](../../decisions/2026-06-29-self-evolution-rollback.md).
> - **Drift response — done, but as reconcile, not refusal.** `ops/controllers/pin.py`
>   tightened the watchdog trigger from schema-drift to SHA-drift exactly as bullet 2
>   below proposed: an off-pin agent-runner force-updates to the pin (backoff-guarded,
>   declines while a cluster update holds the lock); a gateway drift only warns,
>   because it needs the full rollout path.
> - **Still not built:** the *hard* half — a drifted node **refusing work**. But
>   [`2026-07-19-fail-fast-vs-reconcile-boundary.md`](../../decisions/2026-07-19-fail-fast-vs-reconcile-boundary.md)
>   classifies pin drift as **world drift → reconcile toward spec** (no learner in
>   the causal chain, and a refusal converts drift into an outage), which is the
>   opposite posture from the one this doc argues for. So the open question is no
>   longer "build the refusal" but "does anything remain once reconcile is loud and
>   bounded?" — settle that against the later decision first.
>
> The down-migration foundation this rests on (`apply_down` / `rollback_to`) landed
> in #687; the integer down-floor was superseded by the 2026-07-19 re-baseline (the
> squashed `db/schema.sql` is the floor, every post-baseline timestamp migration
> ships a `.down.sql` — see
> [`../../decisions/2026-07-19-migration-timestamp-ids-and-rebaseline.md`](../../decisions/2026-07-19-migration-timestamp-ids-and-rebaseline.md)).
> The rationale for pinning at all is the
> [`philosophy.md`](../../conventions/philosophy.md) "strong invariant over managed
> ambiguity" thread. Triggered by the 2026-06-01 self-upgrade incident.

## Today: schema-level consistency

The cross-node contract is **DB schema version**, not git SHA. Every agent-runner
talks to the central node's DB; `shared/migrations.py:check_schema_version`
asserts `applied == required` (strict, both directions) at every daemon start,
and a `CodeBehindSchema` host self-heals via the watchdog. Nodes may run
*different commits* as long as their schema requirement matches.

This works because no-schema-change commits (prompt / frontend / internal
refactor) do not touch the cross-node contract. But it carries an implicit
**compatibility assumption**: "same schema ⇒ wire-compatible." The gap that
assumption leaves — a `cluster_work` payload whose JSON *semantics* shift without
bumping the SQL schema — is not an implementation miss; it is the model itself
forcing a "what counts as compatible?" judgement. That judgement is exactly what
fail-fast wants gone.

## Direction: pin the whole cluster to one commit

Make "all nodes on the same commit" a hard invariant. Then "are these versions
compatible?" *disappears* instead of being answered — no wire-semantics
judgement, no API-drift edge cases. Justified because Ava is a **closed system**
(every node self-owned), not an open API forced to tolerate client drift.

Commit-pinning is a **superset** of the schema check (same commit ⇒ same schema),
so the schema check stays as the finer-grained diagnostic — it is not removed.

## Hard prerequisite: a reliable, rollback-capable update flow

Commit-level fail-stop means "inconsistent ⇒ the node refuses to run." That only
helps if the update flow is reliable and atomic — otherwise every upgrade becomes
a cluster-wide gamble. The 2026-06-01 incident (a deleted CLI subcommand stranded
the whole cluster ~4h, no auto-rollback) is the proof the flow is **not yet hard
enough**. So this depends on the update flow's failure-path unpause **plus**
rollback-to-last-known-good-SHA on a failed upgrade. Ship that first.

> Update (2026-06-01): **this prerequisite is now satisfied.** Both halves landed
> in candidate 1 — Layer 1 failure-path unpause (#692) + Layer 2
> rollback-to-last-known-good (commit + schema), in `cli/commands/update.py` +
> `cli/commands/_update_recover.py`. Commit-pinning can
> build on the recovery flow now in place; the remaining gate is the **Golden
> Commit G₀** alignment (rolling #687 + 0023 across the cluster), which is an ops
> step, not more code.

## Implementation direction (shape only)

- A single source of truth for `cluster_target_sha` (gateway writes it
  after it upgrades; central DB / Redis).
- Each agent-runner checks `HEAD == cluster_target_sha` at start + periodically;
  mismatch ⇒ fail-fast (refuse work) + watchdog triggers self-update. Reuses the
  existing `CodeBehindSchema → watchdog ava update` skeleton, tightening the
  trigger from schema-drift to SHA-drift.
- A failed upgrade rolls the node back to the last-known-good SHA.

> **Landed (2026-06-02): increment A — persist + visualize.** Bullet 1 is done: a
> single source of truth (`cluster_pin` table + `shared/cluster_pin.py`,
> migration 0026). The gateway writes `cluster_target_sha` after its local
> update reaches the target (`cli/commands/update.py:_persist_cluster_pin`), and
> `ava status` shows each node's HEAD vs the pin (read-only drift surfacing —
> `cli/commands/_probe.py:_cluster_pin_status`). Bullet 2's **fail-fast** half (a
> node refusing work on SHA-drift + the watchdog trigger tightened from schema- to
> SHA-drift) is deferred: it is the load-bearing piece — a bug there strands the
> cluster — so it builds on the now-persisted value once the visibility has shaken
> out. Bullet 3 (rollback to last-known-good) already landed as update-recovery
> Layer 2. A *continuous* node-side check (vs today's on-demand `ava status`) is
> part of that deferred enforcement step.
>
> *(Since superseded in part: the continuous node-side check and the tightened
> watchdog trigger landed as the reconciling `ops/controllers/pin.py`, not as a
> refusal — see the status header.)*

> **First concrete step (2026-06-01):** the SHA-pinned rollout in the update
> orchestration — it resolves one `target_sha` and force-checkouts every node to it
> instead of each re-pulling the moving tip — *is* `cluster_target_sha` in its
> transient (per-rollout) form, plus the update lock. It does not yet persist the
> SHA as a standing invariant or fail-fast on drift between rollouts (that is the
> full model above), but it establishes the threading + force-checkout mechanics
> this builds on, and is motivated by the 2026-06-01 collision.

## Cost / trade-off

Every commit — even prompt-only or frontend-only — would oblige all agent-runners
to move. But: agent-runners are stateless workers (daemon bounce is seconds), the
frontend-only case is a no-rebuild `git pull` on a host that does not serve the
frontend, and routine update already moves the whole cluster together — today's
model just *allows* drift in between. The change is from "tolerate transient
drift" to "forbid it, fail-fast." The win is dissolving a whole class of
compatibility reasoning; the price is that upgrade-flow reliability becomes
load-bearing (see prerequisite).
