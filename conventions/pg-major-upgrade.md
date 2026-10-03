# PostgreSQL major-version upgrade (17 → 18)

How to move a cluster's data plane from the pinned Postgres 17 to 18. Two
paths: **A. `pg_upgrade`** — the data directory is upgraded with both majors'
binaries present (no logical rewrite); **B. rebuild through WAL-G + a logical
move** — restore the physical backup on the old major, dump, load into a
freshly provisioned 18 cluster; the fallback when path A is not viable (missing
extension builds, collation/OS changes, no room for a second copy).

**Status: draft.** The repository pins Postgres 17 in several places; the
upgrade becomes executable only after the approved "expand" change lands. The
places that pin 17 (each needs its 18 counterpart, and the vendored tree is
expanded — a new tree beside the old, never an in-place swap):

- `base/cluster/dataplane/runtime_binaries.py` — `_PG_VERSION`, the zonky
  artifact pins, and the pgvector artifact pins;
- `base/cluster/dataplane/pg_runtime.py` — the installed-runtime acceptance
  (`_require_pg17`) must admit 18 while 17 still runs;
- `base/cluster/dataplane/pg_tools.py` — `PG_BIN_LINUX` / `PG_BIN_WINDOWS`;
- `base/host/system/backend.py` — the Homebrew keg name
  (`brew_prefix("postgresql@17")`);
- `base/host/brew_pin.py` — the approved pin list; and
  `base/host/macos_firewall.py` — the postmaster path allowlist;
- `scripts/provision/database.sh` and `ops/ci_autoscale/cloud-init.yml` — the
  apt/brew package lines;
- `AGENTS.md` — the approved-stable line, after the user approves the bump.

Until that change ships, this document is the target procedure and the
acceptance list; a rehearsal on a scratch home is possible earlier with the
platform packages alone.

## Preconditions (all paths)

- The bump is approved (approved-stable) and the expand change is deployed on
  the target.
- Freeze the home: `ava stop` (a full local stop; agents drain first).
  PgBouncer and Redis are untouched by a Postgres major; no change needed.
- Fresh recovery points: the newest WAL-G base backup recent, the newest
  logical dump verified, and the archive queue drained before the stop (see
  "Before a planned stop" in `runbook.md`). Keep the old major's binaries and
  the old data directory until the bake period ends — they are the rollback.
- Extensions in use are known (`SELECT extname, extversion FROM pg_extension;`)
  and each has files for the new major on this host (pgvector is the one Ava
  provisions; the expand change re-pins it).
- Disk: path A with `--copy` needs room for a second copy of the data
  (`--link` needs almost none but forfeits a plain rollback); path B needs the
  dump plus a fresh 18 data directory.
- **A major upgrade starts a new WAL-G generation.** The prefix names the major
  (`.../pg17/gen1/`). Before the new major serves traffic, point the
  configuration JSON at `.../pg18/gen1/`, run `ava backup walg check`, and take
  a new full backup. Never archive 18 into the old prefix; the old chain stays
  restorable with 17 binaries and is retired separately.

## Path A — pg_upgrade

1. `ava stop`; confirm a clean stop and that the archiver's queue is empty.
2. Install the 18 binaries for this host's resolution mode: the vendored tree
   when the expand change ships it, else the platform packages
   (apt: `postgresql-18` + `postgresql-18-pgvector`; macOS: the
   `postgresql@18` keg — update the pin list). Do not remove the 17 binaries.
3. `initdb` a new data directory beside the live one with the same recipe the
   home's clusters use (same OS user, `UTF8`/`C`; see
   `cli/commands/data_plane/cluster_instance.py`). `pg_upgrade` copies the old
   cluster's configuration files over, and nothing Ava writes lives in the
   data directory (archive settings are launch arguments), so there is nothing
   to hand-merge.
4. Run `pg_upgrade --check` with both binaries and both data directories named;
   resolve everything it reports before touching data.
5. Upgrade: `pg_upgrade --old-bindir <17 bin> --new-bindir <18 bin>
   --old-datadir <$AVA_HOME/pg> --new-datadir <$AVA_HOME/pg-new>` as the OS
   owner. Decide `--copy` vs `--link` consciously: `--link` is fast but leaves
   the old directory unusable as-is.
6. Swap directories on the same filesystem: keep the old one
   (`mv pg pg-17-old; mv pg-new pg`).
7. Switch the WAL-G configuration JSON to the new prefix, then `ava start`
   (converge re-validates the toolchain and the key pin), confirm
   `archive_mode=on`, `ava backup walg check`, and `ava backup walg run` for
   the first full backup of the 18 chain.
8. Post-upgrade hygiene: `vacuumdb --all --analyze-in-stages` (optimizer
   statistics are not carried over), then the acceptance list below.
9. After the bake period, retire `pg-17-old` and the 17 binaries.

## Path B — rebuild through WAL-G + logical move

Use when path A is blocked. The home is frozen in both paths, which makes the
archive the source of truth: after the drain, the newest backup plus its WAL
covers every committed transaction, so no "final backup at the freeze" step is
needed.

1. `ava stop` and let the archiver's queue drain (see the planned-stop note).
2. Scratch restore on 17 (same verb as the weekly drill, newest backup that
   started before the freeze; recovering to the end of the archive):
   `ava backup walg restore --dir <scratch> --backup <name>`. Then start the
   copy per the "Start the copy for inspection" section of
   [`partial-table-restore.md`](partial-table-restore.md) and dump:
   `pg_dump --format=custom --file=<scratch>/ava_main.dump -h "$sock" -p 55444
   ava_main` through the private socket.
3. Move the live directory aside (`mv $AVA_HOME/pg $AVA_HOME/pg-17-old`) and
   bring up the 18 cluster through Ava's own provisioning: `ava start` (the
   missing data directory is `initdb`ed by the same code path as a fresh
   install; roles, the write generation, the schema and its migrations are the
   repository's, not hand-built). Stop the writers again before loading.
4. Load the dump **data-only**, so the schema stays the migration-owned shape:
   `pg_restore --data-only --dbname=<socket URL> <scratch>/ava_main.dump`
   (add `--disable-triggers` only if foreign keys force it, as the admin role).
   Sequence values are data; verify them during acceptance and `setval`
   where needed.
5. Switch the WAL-G configuration to the new prefix; `ava start` fully;
   `ava backup walg check` and `ava backup walg run` for the new chain's first
   full backup.
6. Verify against the acceptance list — including a per-table count comparison
   with the scratch copy (still on disk) for the key tables.

The points that need settling together with the expand change: how the
data-only load interacts with the authority-provisioned roles and grants, and
whether sequence values need explicit `setval` per table (verify both in the
rehearsal).

## Acceptance (both paths)

- `ava status` clean; no pending migrations; the health probe reports the
  archive settings, a healthy archiver and the pinned key.
- Row counts against the source for the key tables (`agents`, `checkpoints`,
  `checkpoint_blobs`, `checkpoint_writes`, `tasks`, `inbound_messages`) and a
  conversation read back through the checkpoint reader — the same check the
  logical and physical drills run.
- `ava backup walg status` green on the new prefix, and the weekly drill is due
  on the new chain (it must pass before the old chain is retired).
- Agents boot and read conversations; one conversation read-back is the spot
  check.

## Rollback

Both paths keep the old directory and binaries. Rolling back means: stop, move
directories back, point the WAL-G configuration at the old prefix, start on the
old major. The old chain still restores to the moment the old major stopped; anything
written while the new major ran exists only in the new chain's prefix, so a
rollback started after that point loses it. A rollback after the new
prefix has accepted WAL has not been rehearsed — treat it as an emergency-only
path, and prefer restoring forward.

## Rehearsal requirements before production

- A scratch home (new `$AVA_HOME`, no secrets shared) exercised through path A
  and path B, with the acceptance list run against it.
- The measured times and every deviation recorded — this document gains the
  rehearsal record; do not run the fleet upgrade before that exists.
