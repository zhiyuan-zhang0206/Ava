"""Migration filename validation and tracked-file enumeration."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from shared.log import logger
from shared.migration_errors import MigrationLayoutError
from shared.platform import CREATE_NO_WINDOW
from shared.runtime_interpreter import WHEEL_RUNTIME
from shared.runtime_migration import ReleaseMigrationContext, installed_migration_paths

# The squashed baseline: one sentinel row that stands in for the entire history
# folded into db/schema.sql. Always a member of `required_migration_set()`; has
# no .down.sql (it is the rollback floor). The all-zeros timestamp sorts before
# every real migration, so it is never selected into a rollback diff.
_BASELINE_NAME = "00000000T000000_baseline"

# Filename schema: `YYYYMMDDTHHMMSS_<kebab-name>.sql`. Timestamp = 8-digit date +
# 'T' + 6-digit time; name = lowercase alnum words joined by single hyphens.
_STEM_RE = r"(\d{8}T\d{6}_[a-z0-9]+(?:-[a-z0-9]+)*)"
_FILENAME_RE = re.compile(rf"^{_STEM_RE}\.sql$")
_DOWN_FILENAME_RE = re.compile(rf"^{_STEM_RE}\.down\.sql$")


def _migrations_dir() -> Path:
    from shared.migrations import MIGRATIONS_DIR  # lazy: sees monkeypatches

    return MIGRATIONS_DIR


def _git_probe(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Read-only git probe: capture output, never raise, never flash a console.

    The three call sites share this shape (argv is repo-internal literals + a
    resolved ref); carries CREATE_NO_WINDOW so a Windows runner stays quiet.
    """
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _migration_stem(filename: str) -> str | None:
    """Return the migration NAME (stem without `.sql`) if `filename` is a valid
    up-migration name, else None. Down files / dotfiles / non-.sql -> None."""
    m = _FILENAME_RE.match(filename)
    return m.group(1) if m else None


def _down_path(name: str) -> Path:
    """Return the `.down.sql` Path for a migration name.

    Raises MigrationLayoutError if absent (a post-baseline migration with no
    down is a layout bug the lint also catches)."""
    migrations_dir = _migrations_dir()
    path = migrations_dir / f"{name}.down.sql"
    if not path.is_file():
        raise MigrationLayoutError(f"no .down.sql for migration {name!r}")
    return path


def _assert_unique(names: Iterable[str]) -> None:
    """Assert migration names are unique. Raises MigrationLayoutError naming a
    collision. Timestamp prefixes make collisions rare, but a hand-copied name
    could still duplicate; the primary key would reject it at apply, so catch it
    earlier."""
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise MigrationLayoutError(f"duplicate migration name: {name!r}")
        seen.add(name)


def _tracked_migration_paths() -> set[Path] | None:
    """The absolute paths under migrations/ that git tracks in this checkout.

    `None` when migrations/ is not inside a git worktree (either `git
    rev-parse` or `git ls-files` failed) — the caller decides; the loader
    fails closed, because applying a migration whose git-tracking status
    cannot be verified is exactly the hole the 2026-08-07 incident opened
    (an untracked file in a tracked checkout was auto-applied).

    The worktree root is located from the migrations dir itself (`git
    rev-parse --show-toplevel` walks up from the cwd), so the check works
    whether migrations/ sits directly under the repo root or deeper. Two
    read-only git calls per listing; `-z` so weird filenames cannot split
    entries.
    """

    migrations_dir = _migrations_dir()
    if WHEEL_RUNTIME:
        return installed_migration_paths(migrations_dir)
    root_result = _git_probe(["-C", str(migrations_dir), "rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return None
    root = Path(root_result.stdout.strip())
    try:
        rel = migrations_dir.resolve().relative_to(root)
    except ValueError:
        return None  # migrations dir outside the worktree — cannot verify
    listing = _git_probe(["-C", str(root), "ls-files", "-z", "--", str(rel)])
    if listing.returncode != 0:
        return None
    return {root / entry for entry in listing.stdout.split("\0") if entry}


def _list_migration_files(
    release: ReleaseMigrationContext | None = None,
) -> list[tuple[str, Path]]:
    """Enumerate the migrations dir, validate layout, return (name, path) sorted
    ascending by name (≈ chronological). An empty dir is valid — a release may
    carry no delta over the baseline.

    Only **git-tracked** files are returned: a migration sitting in migrations/
    that git does not track is not part of this checkout's code, so it must
    never be applied (the 2026-08-07 incident: a migration written into the
    running checkout's migrations/ without being committed was auto-applied by
    the watchdog's self-heal, wedging the cluster). Untracked `.sql` files are
    warned about and skipped. The loader fails closed: a repo root that is not
    a git worktree (tracking unverifiable) raises instead of applying files it
    cannot vouch for.

    Exceptions: `MigrationLayoutError` — dir missing / not a git worktree / a
    tracked filename not matching `YYYYMMDDTHHMMSS_<kebab-name>.sql` / a
    duplicate name.
    """
    migrations_dir = _migrations_dir()
    if not migrations_dir.is_dir():
        raise MigrationLayoutError(f"migrations dir does not exist: {migrations_dir}")

    tracked = set(release.validate(migrations_dir)) if release else _tracked_migration_paths()
    if tracked is None:
        raise MigrationLayoutError(
            f"{migrations_dir} is not inside a git worktree — cannot verify which "
            "migration files are git-tracked; refusing to enumerate. Apply path "
            "must only ever apply files git tracks."
        )

    files: list[tuple[str, Path]] = []
    skipped: list[Path] = []
    for path in sorted(migrations_dir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.name.endswith(".down.sql"):
            continue
        if not path.name.endswith(".sql"):
            continue
        if path not in tracked:
            skipped.append(path)
            continue
        stem = _migration_stem(path.name)
        if stem is None:
            raise MigrationLayoutError(
                f"migration filename does not match YYYYMMDDTHHMMSS_<kebab-name>.sql: {path.name}"
            )
        files.append((stem, path))

    if skipped:
        logger.warning(
            "[migration] skipping {n} untracked file(s) in migrations/ — not in "
            "git, will NOT be applied: {names}",
            n=len(skipped),
            names=", ".join(p.name for p in skipped),
        )
    files.sort(key=lambda f: f[0])
    _assert_unique([n for n, _ in files])
    return files


def untracked_migration_files() -> list[str]:
    """Names of the `.sql` up-files in migrations/ that git does not track.

    The applier (Task #998) only ever applies git-tracked migrations and logs a
    warning when it skips untracked ones; this is the same enumeration exposed
    for an operator-facing surface (the converge step), so a migration written
    into the running checkout without a commit is SEEN at converge time, not
    just logged at apply time. Mirrors the loader's skip rule: `.sql` up-files
    only (`.down.sql` excluded), dotfiles ignored. Empty when migrations/ is
    not inside a git worktree — the loader fails closed there and a warning
    would add nothing.
    """
    migrations_dir = _migrations_dir()
    tracked = _tracked_migration_paths()
    if tracked is None:
        return []
    return sorted(
        path.name
        for path in migrations_dir.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.name.endswith(".sql")
        and not path.name.endswith(".down.sql")
        and path not in tracked
    )


def validate_migration_layout(names: Iterable[str]) -> None:
    """Validate a set of migration filenames (basenames) for the on-disk layout
    invariants — each up-migration matches `YYYYMMDDTHHMMSS_<kebab-name>.sql`,
    names are unique. Pure: no filesystem, no DB, no git. Down files
    (`.down.sql`), dotfiles, and non-`.sql` entries are ignored, mirroring the
    on-disk loader. An empty set (no migrations over the baseline) is valid.
    Raises MigrationLayoutError on the first violation.

    This is the pre-flight a rollout runs to vet its target's migrations/ *before*
    stopping any service: a duplicate / malformed name is otherwise only
    discovered by the loader at boot — after the stop — taking the cluster down.
    """
    stems: list[str] = []
    for name in names:
        if name.startswith(".") or not name.endswith(".sql") or name.endswith(".down.sql"):
            continue
        stem = _migration_stem(name)
        if stem is None:
            raise MigrationLayoutError(
                f"migration filename does not match YYYYMMDDTHHMMSS_<kebab-name>.sql: {name}"
            )
        stems.append(stem)
    _assert_unique(stems)


def validate_migrations_at_ref(ref: str, *, repo_root: Path | None = None) -> None:
    """Validate the migrations/ layout at a git ref without checking it out or
    touching the DB — the pre-flight that lets a rollout "validate before kill".

    Reads the migrations/ basenames from `ref`'s tree via `git ls-tree` (no
    checkout) and runs `validate_migration_layout` on them, so a target commit
    whose migrations/ has a duplicate or malformed name is refused while the
    cluster is still serving its current code — instead of the loader failing at
    boot after every service has already been stopped.

    Raises:
        MigrationLayoutError: layout is broken, OR `ref` / its migrations tree
            cannot be read (missing object, not a git repo).
    """

    root = repo_root if repo_root is not None else _migrations_dir().parent
    result = _git_probe(["ls-tree", "-r", "--name-only", "-z", ref, "--", "migrations"], cwd=root)
    if result.returncode != 0:
        raise MigrationLayoutError(
            f"cannot read migrations/ at git ref {ref!r}: "
            f"{result.stderr.strip() or 'git ls-tree failed'}"
        )
    names = [entry.rsplit("/", 1)[-1] for entry in result.stdout.split("\0") if entry.strip()]
    validate_migration_layout(names)


def required_migration_set() -> set[str]:
    """The set of migration names the code in this checkout expects the DB to
    have applied: the baseline sentinel plus every migration file in migrations/.
    The DB's applied set must equal this (both directions checked)."""
    return {_BASELINE_NAME} | {name for name, _ in _list_migration_files()}
