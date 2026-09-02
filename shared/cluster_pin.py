"""The cluster's pinned commit plus the pending-known-good observation window.

The standing record of which git commit the whole cluster should be on. The
gateway writes it (`set_cluster_target_sha`) after a rollout's local update
reaches its target; any node reads it (`get_cluster_target_sha`) to compare its
own HEAD and surface drift (`ava status`). A single row in `cluster_pin` (central
DB).

`last_known_good_sha` is the automatic rollback anchor. A successful backend
rollout pins its target immediately, but records it as `pending_known_good_sha`
until the health probe has observed a healthy window. Only then does
`promote_pending_known_good_if_ready` advance the anchor. `advance_pin` remains
the immediate-advance primitive for an explicit manual operation.

This is the *persisted* form of the per-rollout `target_sha` (the SHA-pinned
rollout in `gateway/cluster.py` / `cli/commands/update.py`, which threads the SHA
only for the duration of one rollout). It is the first step toward commit-level
pinning (`future/infra/commit-pinned-cluster.md`): persist + visualize now;
fail-fast on drift (a node refusing to run when `HEAD != target_sha`) is a later
step that builds on this standing value.
"""

from __future__ import annotations

from datetime import UTC, datetime

import shared.db


def set_cluster_target_sha(target_sha: str, *, set_by: str | None = None) -> None:
    """Record `target_sha` as the cluster's pinned commit, overwriting the prior
    pin. Called by the gateway after a rollout's local update reaches the
    target. `set_by` is free-form provenance (e.g. `machine:pid`)."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_pin SET target_sha = %s, updated_at = now(), updated_by = %s "
            "WHERE id = 1",
            (target_sha, set_by),
        )
        # The singleton row is seeded by migration 0026 and never deleted, so the
        # UPDATE must hit exactly it — rowcount 0 means the row vanished (operator
        # error), an invariant breach we surface rather than silently no-op.
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")


def set_target_with_pending_known_good(new_target_sha: str, *, set_by: str | None = None) -> None:
    """Pin a successful rollout while deferring its LKG promotion to health probes."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_pin SET target_sha = %s, pending_known_good_sha = %s, "
            "pending_known_good_at = now(), updated_at = now(), updated_by = %s "
            "WHERE id = 1",
            (new_target_sha, new_target_sha, set_by),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")


def get_cluster_target_sha() -> str | None:
    """The cluster's pinned commit, or None if no rollout has pinned one yet.

    None means "the singleton row exists but `target_sha IS NULL`" (no rollout has
    pinned a commit). A *missing* row is an invariant breach (migration 0026 seeds
    it and nothing deletes it), so it raises rather than collapsing to the same
    None — otherwise a vanished row would masquerade as an unset pin and the drift
    net would stay silently blind. Mirrors `set_cluster_target_sha`'s rowcount guard."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT target_sha FROM cluster_pin WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        return row[0]


def get_last_known_good_sha() -> str | None:
    """The last-known-good commit — the automatic rollback target for the
    health-probe / rollback flow. NULL means no known-good has been established
    yet (fresh cluster, or no successful rollout has completed)."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT last_known_good_sha FROM cluster_pin WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        return row[0]


def get_pending_known_good() -> tuple[str, datetime] | None:
    """Return the candidate LKG and its rollout time, or None when none is pending."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pending_known_good_sha, pending_known_good_at FROM cluster_pin WHERE id = 1"
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        if row[0] is None or row[1] is None:
            return None
        return row[0], row[1]


def clear_pending_known_good() -> None:
    """Discard the rollout candidate whose observation window no longer applies."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_pin SET pending_known_good_sha = NULL, pending_known_good_at = NULL "
            "WHERE id = 1"
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")


def promote_pending_known_good_if_ready(*, min_age_s: float) -> bool:
    """Promote a still-current pending target once it has aged through the window.

    The same transaction finalizes a matching incomplete rollout when both
    last-update replicas agree: passing the observation window proves the target
    became known-good after any Phase-B tail self-healed, so its old `INCOMPLETE`
    record must no longer read as an open incident. A stale mirror is logged and
    preserves the record instead; it never blocks advancement of the rollback
    anchor.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT target_sha, pending_known_good_sha, pending_known_good_at "
            "FROM cluster_pin WHERE id = 1 FOR UPDATE"
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        target_sha, pending_sha, pending_at = row
        if pending_sha is None or pending_at is None:
            return False
        if pending_sha != target_sha:
            cur.execute(
                "UPDATE cluster_pin SET pending_known_good_sha = NULL, pending_known_good_at = NULL "
                "WHERE id = 1"
            )
            return False
        if (datetime.now(UTC) - pending_at).total_seconds() < min_age_s:
            return False
        cur.execute(
            "UPDATE cluster_pin SET last_known_good_sha = pending_known_good_sha, "
            "last_known_good_at = now(), pending_known_good_sha = NULL, "
            "pending_known_good_at = NULL WHERE id = 1"
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")
        from shared.last_update import finalize_incomplete_update_after_lkg_advance

        finalize_incomplete_update_after_lkg_advance(target_sha=pending_sha, conn=conn)
        return True


def set_last_known_good_sha(sha: str, *, set_by: str | None = None) -> None:
    """Set `last_known_good_sha` directly — for manual override
    (`ava cluster rollback --set-known-good`). Most callers should use
    `advance_pin` instead, which moves `target_sha` → `last_known_good_sha`
    atomically with a rollout."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_pin SET last_known_good_sha = %s, last_known_good_at = now(), "
            "updated_by = %s "  # last writer wins — the append form grew unbounded (audit 2026-08-08 P2)
            "WHERE id = 1",
            (sha, set_by or "unknown"),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")


def seed_last_known_good_sha_if_null(sha: str, *, set_by: str | None = None) -> bool:
    """Seed `last_known_good_sha` to `sha` only when it is currently NULL; a no-op
    if a known-good is already set. Idempotent. Returns True if it wrote the seed,
    False if a value was already present.

    `last_known_good_sha` is otherwise only advanced by a successful rollout
    (`advance_pin`), so a cluster that has never completed one has it NULL — and
    the automatic rollback aborts with "no rollback target" the first time it
    fires. Seeding on the first successful start floors the rollback at the commit
    the cluster came up healthy on. The caller passes the HEAD sha in (this module
    stays free of the CLI git helpers)."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT last_known_good_sha FROM cluster_pin WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        if row[0] is not None:
            return False
        # Conditional UPDATE (re-checks NULL) so two concurrent seeds can't both
        # write — the second matches zero rows and returns False.
        cur.execute(
            "UPDATE cluster_pin SET last_known_good_sha = %s, last_known_good_at = now(), "
            "updated_by = %s "  # last writer wins — see set_pin
            "WHERE id = 1 AND last_known_good_sha IS NULL",
            (sha, set_by or "unknown"),
        )
        return cur.rowcount == 1


def advance_pin(new_target_sha: str, *, set_by: str | None = None) -> str | None:
    """Immediately advance the cluster pin on an explicit successful rollout: the current
    `target_sha` becomes `last_known_good_sha`, and `new_target_sha` becomes
    the new `target_sha`. Returns the previous `target_sha` (now the new
    `last_known_good_sha`), or None if there was no prior pin.

    This is the immediate-advance form. Ordinary backend rollouts use
    `set_target_with_pending_known_good` so the health probe must first observe
    the new commit before it replaces the rollback anchor."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT target_sha FROM cluster_pin WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        old_target = row[0]
        cur.execute(
            "UPDATE cluster_pin SET last_known_good_sha = target_sha, "
            "last_known_good_at = CASE WHEN target_sha IS NOT NULL THEN now() END, "
            "target_sha = %s, updated_at = now(), updated_by = %s "
            "WHERE id = 1",
            (new_target_sha, set_by),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"cluster_pin singleton row missing (rowcount={cur.rowcount})")
        return old_target
