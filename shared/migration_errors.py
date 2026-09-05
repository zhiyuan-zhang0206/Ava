"""Schema migration error hierarchy."""


class MigrationError(Exception):
    """Parent class of Migration runner errors. Broad catch uses this;
    fine catch uses the subclasses."""


class SchemaVersionMismatch(MigrationError):  # noqa: N818 — "state description" naming, same as AgentNotFound
    """DB is missing migrations the code carries — run `ava cluster update`
    (or `ava start`, which applies pending migrations) to catch up."""


class CodeBehindSchema(MigrationError):  # noqa: N818
    """DB has applied migrations this checkout does **not** carry — local
    code is stale; the central DB has already been migrated. Multi-
    machine: an agent-runner missed `ava cluster update` phase B fan-out while
    offline; on coming online, local code is still on the old SHA.

    When the watchdog's schema controller sees this exception, it spawns a
    detached `ava cluster update` to self-heal (see
    `ops/controllers/schema.py:schema_reconcile`).
    """


class MigrationFailed(MigrationError):  # noqa: N818
    """A migration file's SQL erred during apply. Wraps the original
    psycopg exception; `__cause__` carries the original."""


class MigrationLayoutError(MigrationError):
    """`migrations/` directory layout error — dir missing / a filename that
    does not match the timestamp format / a duplicate name."""


class RollbackBelowFloor(MigrationError):  # noqa: N818 — state-description naming
    """Rollback target is below the baseline; the baseline has no down by
    design (it is a squashed snapshot). Rolling back across it would strand a
    set-tracked DB under pre-cutover integer-tracked code."""


class CutoverRequired(MigrationError):  # noqa: N818
    """A read path saw a legacy (sequential-integer) `schema_migrations` that
    has not been converted to the applied-set format yet. On the shared prod
    DB the gateway's `ava cluster update` runs the conversion; an agent-runner that
    sees this must wait for the gateway, not self-update."""


class CutoverRefused(MigrationError):  # noqa: N818
    """The apply path found a legacy `schema_migrations` whose applied set is
    NOT the exact `{1..81}` baseline (behind, or gaps), so it cannot be safely
    folded into the squashed baseline. Step through the immediately-preceding
    release first."""


class MigrationHistoryGap(MigrationError):  # noqa: N818
    """The apply path found a DB that applied only PART of the pre-v0.1.0
    migration history (some of `_V010_PRE_RESET_SET` present, some missing).
    Convergence deletes applied-set rows whose files no longer exist — for a
    partial history that would certify a schema that never ran the missing
    migrations. Step the DB through a pre-reset release first so it reaches
    the full set, then upgrade across the reset."""


class MigrationAuthorityMismatch(MigrationError):  # noqa: N818
    """The executing checkout is not the gateway unit of the cluster whose DB it
    is about to migrate, so it may not mutate that schema.

    A cluster's schema is owned by its gateway unit: `ava cluster update` there is the
    single orchestrator that quiesces agents, migrates, and rolls the new commit
    out. Anyone else reaching the same DB with a newer `migrations/` migrates it
    out from under the gateway, which then fails its own startup schema check
    (`CodeBehindSchema`) and rejects every agent boot — the 2026-07-31 wedge.

    Two paths reach here, both real:
    - an **agent-runner**, whose `ava start` applies migrations against the
      central DB it shares with the gateway; its checkout races ahead whenever
      it self-heals past the cluster pin.
    - a **dev worktree** whose process inherited `AVA_HOME=~/.ava` (every agent
      prod launches does), pointing its DB URL at prod while
      `MIGRATIONS_DIR` still resolves to the worktree's own files.
    """
