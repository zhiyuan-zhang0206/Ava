"""Schema migration runner — framework infrastructure shared by gateway / agent.

Lives in `shared/` because gateway / service daemons / agent host
all need to call `assert_schema_current()` at their entry points for
schema sanity; unrelated to the agent SDK (`ava.*`); we do not want
gateway processes reverse-depending on the `ava` package.

Long-running processes (gateway / agent-host /
labeler) call `assert_schema_current()` early in startup;
`ava cluster update` flow calls `apply_pending_migrations()` after git
pull.

Migrations are applied as a step of `ava start`; `ava cluster update` triggers it on
new code.

Identity model (2026-07-19 timestamp-id + re-baseline cutover):
- File layout: `<repo-root>/migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql` — a
  second-precision UTC timestamp prefix + a kebab-case name, body is raw SQL
  (`apply_pending_migrations` auto-wraps in a transaction). The paired
  `.down.sql` reverses it. The timestamp makes names collision-free without a
  coordinating counter, so parallel branches never fight over the "next number"
  (the 0060 / 0062 / 0080 collision incidents).
- Tracking is an **applied set**, not a high-water integer: `schema_migrations`
  is keyed by migration NAME (`name TEXT PRIMARY KEY, applied_at`). Apply =
  apply every migration file whose name is not yet in the set, in name
  (≈ chronological) order; an out-of-order merge is handled by definition — each
  name is tracked independently, so there is no contiguity to violate.
- The **baseline** (`_BASELINE_NAME`) is a squashed snapshot: `db/schema.sql`
  is the current full schema and stamps this one sentinel row on a fresh DB
  instead of replaying history. `required_migration_set()` always includes it,
  so a DB missing the baseline is "not provisioned". It has no `.down.sql` — it
  is the down-migration floor; a rollback that would remove it is refused
  (`RollbackBelowFloor`).

Cutover from the pre-2026-07-19 sequential-integer scheme (`schema_migrations`
was `version INT`, seeded `generate_series(1, 81)`) is one-way and automatic:
`_ensure_cutover` (run inside `apply_pending_migrations`, under the mutation
lock) converts a DB whose legacy applied set is exactly `{1..81}` into the
baseline. Any other legacy state (behind 81, or gaps) is refused with a
"step through the immediately-preceding release first" error — that release
brings the DB to the full 1..81 baseline this squash assumes.

Design points:
- Startup check **compares but does not apply** — apply is an explicit step of
  the update flow; startup auto-apply races at multi-process start and breaks
  the single-orchestrator invariant.
- Apply is **gateway-only**, enforced against the DB's own identity: a cluster's
  schema belongs to the unit `machine_units` records as gateway-capable, and
  `_assert_migration_authority` refuses any other checkout that reaches the same
  database with pending migrations. Without it, every host sharing a central DB
  (agent-runners, and worktree processes that inherited `AVA_HOME`) could
  migrate it out from under the gateway — the 2026-07-31 wedge.
- Each migration runs in one transaction: most PG DDL is transactional; body
  failure rolls back; schema state unchanged.
- The runner INSERTs the name; **migration files must not INSERT themselves**
  (would collide on the primary key). The sole exception: `db/schema.sql`
  fresh-DB bootstrap stamps the baseline row itself and does not go through
  this runner.
- fail fast: dir missing / a filename not matching the timestamp format / a
  duplicate name -> immediately raise a specific subclass, no silent fallback.
  An **empty** `migrations/` is valid now (a release with no delta over the
  baseline).
- validate before kill: `validate_migrations_at_ref()` vets a rollout target's
  migrations/ layout from git (no checkout, no DB) so a duplicate / malformed
  name is refused while the cluster still serves its current code -- instead of
  the loader only catching it at boot, after every service has been stopped.
- git tracking is the source of truth for what may be applied: a migration file
  that git does not track is warned about and skipped (2026-08-07 incident: a
  migration written into the running checkout's migrations/ without a commit
  was auto-applied by the watchdog's self-heal, wedging the cluster). The
  loader fails closed — if the repo root is not a git worktree it refuses to
  enumerate rather than apply files it cannot verify.
"""

from __future__ import annotations

import contextlib
import re as re
import subprocess as subprocess
from collections.abc import Generator
from collections.abc import Iterable as Iterable
from pathlib import Path

import psycopg

from shared.db import PG_KEEPALIVE_KWARGS
from shared.dotenv_boot import checkout_anchored_home
from shared.log import logger
from shared.machine import MachineNameMissing, machine_name
from shared.migration_errors import CodeBehindSchema as CodeBehindSchema
from shared.migration_errors import CutoverRefused as CutoverRefused
from shared.migration_errors import CutoverRequired as CutoverRequired
from shared.migration_errors import MigrationAuthorityMismatch as MigrationAuthorityMismatch
from shared.migration_errors import MigrationError as MigrationError
from shared.migration_errors import MigrationFailed as MigrationFailed
from shared.migration_errors import MigrationHistoryGap as MigrationHistoryGap
from shared.migration_errors import MigrationLayoutError as MigrationLayoutError
from shared.migration_errors import RollbackBelowFloor as RollbackBelowFloor
from shared.migration_errors import SchemaVersionMismatch as SchemaVersionMismatch
from shared.migration_layout import _BASELINE_NAME as _BASELINE_NAME
from shared.migration_layout import _DOWN_FILENAME_RE as _DOWN_FILENAME_RE
from shared.migration_layout import _FILENAME_RE as _FILENAME_RE
from shared.migration_layout import _STEM_RE as _STEM_RE
from shared.migration_layout import _assert_unique as _assert_unique
from shared.migration_layout import _down_path as _down_path
from shared.migration_layout import _git_probe as _git_probe
from shared.migration_layout import _list_migration_files as _list_migration_files
from shared.migration_layout import _migration_stem as _migration_stem
from shared.migration_layout import _tracked_migration_paths as _tracked_migration_paths
from shared.migration_layout import required_migration_set as required_migration_set
from shared.migration_layout import untracked_migration_files as untracked_migration_files
from shared.migration_layout import validate_migration_layout as validate_migration_layout
from shared.migration_layout import validate_migrations_at_ref as validate_migrations_at_ref
from shared.platform import CREATE_NO_WINDOW as CREATE_NO_WINDOW
from shared.runtime_interpreter import WHEEL_RUNTIME as WHEEL_RUNTIME
from shared.runtime_migration import ReleaseMigrationContext
from shared.runtime_migration import installed_migration_paths as installed_migration_paths

# Repo root = shared/.. = `<root>/`; migrations dir is under repo root.
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent.parent / "migrations"

# Fixed key for the Postgres advisory lock that serializes the whole apply loop
# (see `_schema_mutation_lock`). Arbitrary but stable cluster-wide; ASCII "AVMI".
_MIGRATION_LOCK_KEY = 0x41564D49

# The legacy sequential-integer schema version that db/schema.sql squashes. A
# pre-cutover DB is convertible only if its applied set is exactly {1.._LEGACY_
# BASELINE_MAX}; see `_ensure_cutover`. Frozen at cutover time — do not bump.
_LEGACY_BASELINE_MAX = 81
_LEGACY_BASELINE_SET = frozenset(range(1, _LEGACY_BASELINE_MAX + 1))

# The 59 timestamped migrations squashed into db/schema.sql at the v0.1.0
# release (2026-08-14 schema reset). Frozen at reset time — do not bump. A DB
# whose applied set contains ANY of these must contain ALL of them before the
# convergence path may delete their tracking rows (see apply_pending_migrations
# and MigrationHistoryGap); a partial set means schema effects that never ran.
_V010_PRE_RESET_SET = frozenset(
    {
        "20260719T223436_root-task-default-parent",
        "20260720T050943_agents-meta-last-active-at",
        "20260720T191255_agents-meta-hibernating-status",
        "20260721T042152_drop-inbound-notify-trigger",
        "20260721T082401_agent-notices-task-id",
        "20260721T082402_agent-tasks-priority",
        "20260721T090000_agents-meta-termination-source",
        "20260722T051500_agent-events-rollup-tables",
        "20260722T072626_agent-events-monthly-partitioning",
        "20260723T023228_add-explorer-preset",
        "20260725T025418_task-reminder-column-rename",
        "20260725T054607_rename-skills-ava-prefix",
        "20260725T060802_pin-haiku-dated-model-id",
        "20260725T074822_rename-skills-ava-prefix-round2",
        "20260728T055350_add-last-wedged-check-at",
        "20260729T041500_cluster-update-lock-note",
        "20260729T093000_termination-source-integrity",
        "20260731T042431_skill-names-dash-canonical",
        "20260731T071400_agent-birth-config",
        "20260731T071500_cluster-defaults",
        "20260731T071600_default-model-deepseek-v4-flash",
        "20260731T084500_seed-presets-drop-skill-index-list",
        "20260731T151000_cluster-last-update",
        "20260801T041104_up-since-at-expand",
        "20260802T202812_inbound-claimed-at",
        "20260803T180647_ops-alert-rules",
        "20260803T181500_ops-metrics-table",
        "20260804T190513_retire-ops-alert-rules",
        "20260804T190839_unified-events-table",
        "20260804T203036_events-readers-neighbors",
        "20260804T214534_ops-alerts",
        "20260805T001003_ops-alerts-source",
        "20260805T083741_kind-category-final",
        "20260807T010148_events-kind-to-event-name",
        "20260807T040600_delivery-alerted-dedup",
        "20260807T054700_pages-serve-dir-reopen",
        "20260807T083219_cluster-ops-idempotency",
        "20260807T183600_skill-names-canonical-three-store",
        "20260807T213500_api-idempotency",
        "20260808T043000_r1-deploy-state-tables",
        "20260808T073335_r1-host-paused-at",
        "20260808T075000_skill-identity-config-refs",
        "20260808T104500_agent-watchers",
        "20260808T184958_select-all-lateral-indexes",
        "20260808T200000_unify-ops-idempotency",
        "20260808T203000_agent-tasks-constraints",
        "20260809T030358_fyi-answerable",
        "20260810T140000_blob-autovacuum-tuning",
        "20260810T224124_events-level-index-includes-critical",
        "20260810T224356_schedule-runs-agent-fk",
        "20260811T050000_contract-sweep-dead-tables",
        "20260811T051000_agent-watchers-composite-pk",
        "20260812T000000_machines-is-staging",
        "20260812T040636_agent-liveness-state",
        "20260812T230738_drop-ops-metrics",
        "20260813T042527_alerts",
        "20260813T231327_events-target-agent-id-index",
        "20260814T092155_llm-usage-cost-snapshot",
        "20260814T182039_machines-pause",
    }
)


def _schema_migrations_shape(conn: psycopg.Connection) -> str:
    """Classify the DB's schema_migrations table: 'absent' (no table), 'set'
    (new applied-set format, keyed by `name`), or 'legacy' (pre-cutover
    sequential-integer format, keyed by `version`)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='schema_migrations'"
        )
        cols = {r[0] for r in cur.fetchall()}
    if not cols:
        return "absent"
    if "name" in cols:
        return "set"
    if "version" in cols:
        return "legacy"
    raise MigrationLayoutError(
        f"schema_migrations has an unrecognized shape (columns: {sorted(cols)}); "
        "expected a `name` (applied-set) or `version` (legacy) column"
    )


def _applied_migration_set(conn: psycopg.Connection) -> set[str]:
    """Read the applied-migration NAME set from schema_migrations.

    Table absent (a DB that has never been provisioned) -> empty set; the caller
    decides whether that is behind-code (check) or nothing-to-do (apply). A
    legacy (pre-cutover) table raises `CutoverRequired`: the apply path converts
    it before reading (via `_ensure_cutover`), so only a read path reaches here
    on legacy — and a read must never silently mutate.
    """
    shape = _schema_migrations_shape(conn)
    if shape == "absent":
        return set()
    if shape == "legacy":
        raise CutoverRequired(
            "schema_migrations is still in the pre-cutover integer format. On the "
            "shared prod DB the gateway's `ava cluster update` runs the conversion; if this "
            "is an agent-runner, wait for the gateway to update. If the gateway "
            "already updated, the conversion did not run — investigate before "
            "starting services against it."
        )
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}


def applied_migration_names(conn: psycopg.Connection) -> set[str]:
    """The DB's applied-migration name set — the snapshot `ava cluster update` captures
    right before it applies a batch, so a failed start can roll the schema back
    to exactly the pre-update set (`rollback_to`)."""
    return _applied_migration_set(conn)


def check_schema_version(conn: psycopg.Connection) -> None:
    """Pre-startup sanity: the DB's applied set **must equal** the code's
    required set (baseline + migration files). Both directions are errors:

    - required minus applied non-empty (DB missing migrations the code carries):
      gateway single-host / no migration run yet. Raises `SchemaVersionMismatch`;
      run `ava cluster update` (or `ava start`, which applies pending) to catch up.
    - applied minus required non-empty (DB has migrations this checkout lacks): an
      agent-runner missed the gateway's `ava cluster update` phase B (e.g. while
      offline). Raises `CodeBehindSchema`; the watchdog's schema controller
      auto-spawns `ava cluster update` (on an agent-runner that is the self-update leg:
      git checkout + uv sync + restart, no migrations) to self-heal.

    A DB that is simultaneously ahead AND behind (both diffs non-empty) is a true
    divergence; it raises `CodeBehindSchema` (a set the code cannot have produced
    is the more urgent, less recoverable signal) with both diffs listed.

    Set equality rather than a high-water compare, because code newer than DB is
    also a bug (dev-time staged migration run but not committed; cross-machine
    inconsistency).

    Exceptions:
        SchemaVersionMismatch: DB behind code.
        CodeBehindSchema: DB ahead of code (or divergent).
        CutoverRequired: DB still in the pre-cutover integer format.
        MigrationLayoutError: migrations/ layout itself is broken.
    """
    required = required_migration_set()
    applied = _applied_migration_set(conn)
    missing = required - applied  # DB behind code
    extra = applied - required  # DB ahead of code
    if extra:
        detail = f"DB has {len(extra)} migration(s) this checkout lacks: {sorted(extra)}"
        if missing:
            detail += f"; and is missing {len(missing)}: {sorted(missing)} (divergent)"
        raise CodeBehindSchema(
            f"Schema ahead of code: {detail}. Run `ava cluster update` — on an "
            "agent-runner that is the self-heal (checkout + uv sync + restart); "
            "on the gateway it pulls, migrates, and rolls out the cluster."
        )
    if missing:
        raise SchemaVersionMismatch(
            f"Schema behind code: DB is missing {len(missing)} migration(s): "
            f"{sorted(missing)}. Run `ava cluster update` (or `ava start`, which applies "
            "pending migrations) to catch up."
        )


def assert_schema_current(db_url: str) -> None:
    """Convenience function — for long-running daemon startup
    entries: open a short-lived connection and run
    check_schema_version.

    Exceptions SchemaVersionMismatch / CodeBehindSchema / CutoverRequired /
    MigrationLayoutError pass through; the process entry receives the traceback
    and exits directly; the human/Claude reads the stack and takes over.

    Connects with `shared.db.PG_KEEPALIVE_KWARGS` rather than through
    `shared.db.connect()`: no function in this module reads settings — every
    other entry point takes a `conn` and this one takes `db_url` — whereas
    `connect()` reads `settings.data_plane` and would ignore the argument.
    Every caller passes `settings.data_plane.db_url` — the one URL — which
    points at the PgBouncer pooler when pooling is on; this boot assertion is
    a read-only SELECT, so pooled is fine (only the migration applier needs
    the direct URL).
    The kwargs come from that one constant so there is a single definition of the
    cluster's connect-timeout / keepalive posture. `connect_timeout` is the
    load-bearing one here: this is the FIRST thing a daemon does at boot, so
    against a database that black-holes packets (dropped traffic, not
    ECONNREFUSED — a runner that changed networks, a stale route) a bare connect
    parks the whole boot on the OS TCP-retransmit timeout. The daemon then reads
    as "failed to start" while it is really blocked on a socket, and
    `respawn_and_verify` reports it down and respawns another one that wedges
    identically. Bounded at 5s, boot raises the socket error instead.
    """
    with psycopg.connect(db_url, autocommit=True, **PG_KEEPALIVE_KWARGS) as conn:
        check_schema_version(conn)


@contextlib.contextmanager
def _schema_mutation_lock(conn: psycopg.Connection) -> Generator[None]:
    """Hold a Postgres advisory lock for a whole schema-mutation loop (cutover,
    forward apply, or rollback).

    Serializes *every* path that mutates the schema — a rollout's
    `_run_gateway_local_update`, a manual / watchdog `ava start`, a recovery
    `rollback_to` — on one key, so a second mutator blocks until the first
    finishes instead of racing and losing on the `schema_migrations` primary key.
    Independent of the TTL'd cluster-update lock (`shared.cluster_lock`): that one
    serializes whole rollout orchestrations; this one guards the mutation step
    itself, so even non-orchestration mutators (the bootstrap `ava start`) are safe
    without entangling bootstrap with the rollout lock.

    Taken on the **writer connection itself** (`pg_advisory_lock`, session-level),
    so the lock's lifetime is exactly the connection doing the writes: it cannot be
    silently lost while writes continue (a separate lock session could be killed
    out from under a live writer), and it auto-releases if that connection's session
    ends (crash-safe, no TTL needed). Session-level means it spans — and survives
    the commits/rollbacks of — the per-migration transactions the loop opens on the
    same conn; released explicitly on exit, and by the connection closing otherwise.
    Reentrant per session, so the writer's own statements never self-conflict."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))


def _ensure_cutover(
    conn: psycopg.Connection, release: ReleaseMigrationContext | None = None
) -> None:
    """One-way convert a legacy (sequential-integer) schema_migrations into the
    applied-set baseline. Idempotent: a new-format or absent table is a no-op.
    Must run under `_schema_mutation_lock` on a non-autocommit conn (the caller,
    `apply_pending_migrations`, holds it).

    A legacy DB is convertible only if its applied set is exactly the `{1..81}`
    baseline that db/schema.sql squashes. Any other legacy state (behind 81, or
    gaps) raises `CutoverRefused` — the operator must step through the
    immediately-preceding release, which advances the DB to the full 1..81
    baseline, before this one.

    Exceptions:
        CutoverRefused: legacy applied set is not the exact {1..81} baseline.
    """
    if _schema_migrations_shape(conn) != "legacy":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        applied: frozenset[int] = frozenset(r[0] for r in cur.fetchall())
    if applied != _LEGACY_BASELINE_SET:
        missing = sorted(_LEGACY_BASELINE_SET - applied)
        extra = sorted(applied - _LEGACY_BASELINE_SET)
        raise CutoverRefused(
            "cannot convert legacy schema_migrations to the applied-set baseline: "
            f"applied versions are not exactly 1..{_LEGACY_BASELINE_MAX} "
            f"(missing={missing}, extra={extra}). Upgrade to the release "
            "immediately preceding this one (the last with sequential-integer "
            f"migrations, schema version {_LEGACY_BASELINE_MAX}) so the DB reaches "
            "the full baseline, then upgrade to this release."
        )
    if release is None:
        _assert_migration_authority(conn)
    else:
        _assert_migration_authority(conn, release)
    logger.info(
        "[migration] converting legacy schema_migrations (1..{max}) -> baseline",
        max=_LEGACY_BASELINE_MAX,
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DROP TABLE schema_migrations")
        cur.execute(
            "CREATE TABLE schema_migrations ("
            "  name TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        )
        cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_BASELINE_NAME,))
    logger.info("[migration] cutover complete; baseline stamped")


def _gateway_units(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """The `(machine_name, home)` of every unit this DB records as gateway-capable
    — the cluster's own identity, as written by `register_self` at `ava start`.

    Empty means the DB carries no identity yet: a fresh birth migrates at
    `ava start` step 2.5, before step 3 writes machine_units. `to_regclass`
    rather than a catch: a missing table on a pre-provision DB is an expected
    state, and probing the catalog keeps the surrounding transaction clean.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.machine_units')")
        row = cur.fetchone()
        if row is None or row[0] is None:
            return []
        cur.execute(
            "SELECT machine_name, home FROM machine_units "
            "WHERE serve_gateway ORDER BY machine_name, home"
        )
        return [(name, home) for name, home in cur.fetchall()]


def _assert_migration_authority(
    conn: psycopg.Connection, release: ReleaseMigrationContext | None = None
) -> None:
    """Refuse unless this checkout is the gateway unit of the cluster it is about
    to migrate. Raises MigrationAuthorityMismatch naming both identities.

    The executing identity is deliberately `checkout_anchored_home()`, NOT the
    env-resolved home: a worktree process that inherited `AVA_HOME=~/.ava` has a
    prod DB URL *and* a prod-looking `ava_home()`, so only the checkout's own
    claim distinguishes it. An unanchored checkout cannot prove ownership at all
    and is refused on that ground.
    """
    units = _gateway_units(conn)
    if not units and release is None:
        return
    home, anchored = (release.home, True) if release else checkout_anchored_home()
    try:
        this_machine = machine_name()
    except MachineNameMissing:
        this_machine = "<unset>"
    if anchored and (this_machine, str(home)) in units:
        return
    owner = ", ".join(f"{name}:{path}" for name, path in units)
    claim = f"{this_machine}:{home}" if anchored else f"{this_machine}:<unanchored checkout>"
    raise MigrationAuthorityMismatch(
        f"refusing to migrate: this checkout claims {claim}, but the database's "
        f"gateway unit is {owner}. A cluster's schema is owned by its gateway — "
        f"run `ava cluster update` there. Applying {MIGRATIONS_DIR} would leave the "
        f"gateway's own code behind the schema and wedge every agent boot."
    )


def apply_pending_migrations(
    conn: psycopg.Connection, *, release: ReleaseMigrationContext | None = None
) -> list[str]:
    """Apply every git-tracked migration file whose name is not yet in the DB's
    applied set, in name (≈ chronological) order; return the list of names
    actually applied. Untracked files in migrations/ are warned about and
    skipped (see `_list_migration_files`).

    First converts a legacy DB via `_ensure_cutover` (no-op on a new-format /
    absent table). Each migration runs in one transaction (`conn.transaction()`);
    body SQL and the INSERT are in the same transaction; failure rolls back
    together -> schema state does not advance.

    The whole loop runs under `_schema_mutation_lock(conn)` (a Postgres advisory
    lock on this connection) so concurrent mutators serialize on the schema rather
    than the loser failing on the schema_migrations primary key.

    Requires conn to be **non-autocommit** — `conn.transaction()` ctx needs it to
    manage the transaction. `ava.DB` defaults to autocommit=True; this function's
    caller should pass an independent psycopg.connect() conn (autocommit=False,
    default).

    Before applying anything (and only then — see the comment at the call site)
    `_assert_migration_authority` requires this checkout to be the cluster's
    gateway unit, so no other checkout sharing the DB can migrate it.

    Exceptions:
        MigrationFailed: SQL failed; `__cause__` is the original psycopg exception.
        CutoverRefused: legacy DB not at the exact baseline (from `_ensure_cutover`).
        MigrationLayoutError: layout broken.
        MigrationAuthorityMismatch: this checkout does not own the cluster.
    """
    with _schema_mutation_lock(conn):
        if release is not None:
            release.assert_operation(conn)
            release.validate(MIGRATIONS_DIR)
            _assert_migration_authority(conn, release)
        if release is None:
            _ensure_cutover(conn)
            files = _list_migration_files()
        else:
            _ensure_cutover(conn, release)
            files = _list_migration_files(release)
        applied = _applied_migration_set(conn)
        required = {_BASELINE_NAME} | {name for name, _ in files}

        # Re-baseline convergence: applied names whose migration file no longer
        # exists (and which are not the baseline) have been squashed into
        # db/schema.sql by a re-baseline — drop their tracking rows so the
        # applied set matches what this code expects. The convergence gate
        # (scripts/test_migrations_apply.sh) proves db/schema.sql is the net
        # effect of every migration file, so a missing file means "folded into
        # the baseline", never "lost". A rolled-back cluster self-heals: the
        # older code's apply re-runs its (idempotent) migration files against
        # the already-current schema and restores the names.
        squash = applied - required

        # P1 guard (adversarial review 2026-08-11): never converge a DB that
        # applied only PART of the pre-reset history. A partial set means the
        # missing migrations' schema effects never ran; deleting the present
        # names' tracking rows would make the applied set EQUAL the required
        # set (check passes) while the schema is not the baseline — a silent
        # divergence that only explodes at runtime. The full set is safe (the
        # baseline carries its net effect, proven by the convergence gate); an
        # empty set is a fresh or already-converged DB.
        pre_reset_applied = applied & _V010_PRE_RESET_SET
        pre_reset_missing = _V010_PRE_RESET_SET - applied
        if pre_reset_applied and pre_reset_missing:
            raise MigrationHistoryGap(
                "cannot converge: DB applied only "
                f"{len(pre_reset_applied)}/{len(_V010_PRE_RESET_SET)} pre-v0.1.0 "
                f"migrations (missing={sorted(pre_reset_missing)[:5]}{'...' if len(pre_reset_missing) > 5 else ''}). "
                "Upgrade through a pre-reset release first so the full history "
                "is applied, then upgrade across the reset."
            )

        # Authority is checked only when there is something to mutate, so an
        # agent-runner's `ava start` — which legitimately calls this on the
        # cluster's central DB and normally applies nothing — keeps working. The
        # refusal fires exactly when a non-gateway checkout would mutate the
        # schema (a squash is a mutation too).
        pending = [(name, path) for name, path in files if name not in applied]
        if pending or squash:
            if release is None:
                _assert_migration_authority(conn)
            else:
                _assert_migration_authority(conn, release)

        if squash:
            logger.info(
                "[migration] squashing {n} applied name(s) into the baseline "
                "(files no longer exist; schema folded into db/schema.sql): {names}",
                n=len(squash),
                names=", ".join(sorted(squash)),
            )
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM schema_migrations WHERE name = ANY(%s)",
                    (sorted(squash),),
                )
            applied -= squash

        applied_now: list[str] = []
        for name, path in pending:
            body = path.read_text()
            # Bracket each migration: a hang or kill mid-apply must leave the
            # log naming WHICH migration was in flight (the applying line with
            # no matching applied line), not just whatever the caller prints
            # after the whole loop.
            logger.info("[migration] {name} applying...", name=name)
            try:
                with conn.transaction(), conn.cursor() as cur:
                    # prepare=False: a migration body is arbitrary, possibly
                    # MULTI-statement SQL, which cannot go through the extended
                    # (prepared-statement) protocol — Postgres rejects "cannot
                    # insert multiple commands into a prepared statement". The
                    # connection may carry prepare_threshold=0 (shared.db.connect
                    # sets it unconditionally, for PgBouncer transaction-pool
                    # safety), which would otherwise force preparation on the first
                    # execute and break every multi-statement migration. Forcing
                    # the simple protocol here keeps the applier independent of the
                    # conn's prepare posture.
                    cur.execute(body, prepare=False)  # type: ignore[arg-type]
                    cur.execute(
                        "INSERT INTO schema_migrations (name) VALUES (%s)",
                        (name,),
                    )
            except psycopg.Error as exc:
                raise MigrationFailed(f"apply migration {name} failed: {exc}") from exc
            logger.info("[migration] {name} applied", name=name)
            applied_now.append(name)
        return applied_now


def apply_down(conn: psycopg.Connection, name: str) -> None:
    """Run a migration's `.down.sql` + delete its schema_migrations row, one
    transaction (the reverse of forward apply). Requires a non-autocommit conn.
    Inside `rollback_to`'s single rollback transaction, this transaction is a
    savepoint; standalone, it is its own transaction.

    Exceptions:
        MigrationFailed: the down SQL failed; `__cause__` is the psycopg error.
        MigrationLayoutError: no `.down.sql` for this name.
    """
    path = _down_path(name)
    body = path.read_text()
    logger.info("[migration] {name} rolling back...", name=name)
    try:
        with conn.transaction(), conn.cursor() as cur:
            # prepare=False: same reason as the forward apply — a down body may be
            # multi-statement, which the prepared-statement protocol rejects, and
            # the conn may carry prepare_threshold=0.
            cur.execute(body, prepare=False)  # type: ignore[arg-type]
            cur.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
    except psycopg.Error as exc:
        raise MigrationFailed(f"down migration {name} ({path.name}) failed: {exc}") from exc
    logger.info("[migration] {name} rolled back", name=name)


def rollback_to(conn: psycopg.Connection, keep: set[str]) -> list[str]:
    """Roll back every applied migration NOT in `keep` (the target's required
    set), in reverse-name (≈ reverse-chronological) order; return the names
    rolled back.

    `keep` is the applied set the DB should have after the rollback — typically
    the snapshot from `applied_migration_names` (recovery) or the target commit's
    migration set (`ava cluster rollback`). Rolling back the baseline sentinel is
    refused: the baseline has no down, and crossing it would strand a set-tracked
    DB under pre-cutover code.

    The whole rollback runs in one transaction. If any down fails, the batch
    aborts atomically and leaves the schema and applied set unchanged, so the
    caller can fix-forward safely.

    Exceptions:
        RollbackBelowFloor: the rollback set includes the baseline (target
            predates the cutover).
        MigrationFailed / MigrationLayoutError: from `apply_down`.
    """
    with _schema_mutation_lock(conn):
        rolled: list[str] = []
        with conn.transaction():
            applied = _applied_migration_set(conn)
            to_roll = applied - keep
            if _BASELINE_NAME in to_roll:
                raise RollbackBelowFloor(
                    "rollback target is below the squashed baseline (the baseline has "
                    "no down migration). Choose a target at or after the re-baseline "
                    "cutover, or fix-forward."
                )
            for name in sorted(to_roll, reverse=True):
                apply_down(conn, name)
                rolled.append(name)
    return rolled
