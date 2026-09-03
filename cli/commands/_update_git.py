"""git pull + pending-migration helpers for `ava cluster update` (formerly cli/upgrade.py).

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. `ava cluster update` runs git in a detached subprocess spawned by the gateway;
that child is what imports these helpers — the agent SDK path does not call them
directly. Re-imported by `cli/commands/update.py` (and re-exported through
`cli.commands`) so existing `cli.commands.git_pull_main` /
`cli.commands.update.git_pull_main` references keep resolving.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import shared.pg_tools
from cli.commands._repo import _repo_root
from shared.config import settings
from shared.gitenv import git_env
from shared.proc import run_bounded
from shared.runtime_migration import ReleaseMigrationContext

_REPO_ROOT_FOR_GIT = _repo_root()


def _tracking_remote_ref() -> str:
    """The remote tracking ref this cluster pulls from: `origin/<track_branch>`.
    `track_branch` defaults to 'main' (production); preview/staging clusters
    override it via `AVA_TRACK_BRANCH`."""
    return f"origin/{settings.general.track_branch}"


def track_fetch() -> None:
    """Fetch the track target's objects — the precondition for resolving it.

    `latest` mode fetches the track branch; `releases` mode additionally
    fetches tags, because the newest dated release tag may not be reachable by
    branch auto-follow (a tag on an older main). Read-only; never mutates the
    working tree.
    """
    _git_network("fetch", "origin", settings.general.track_branch)
    if settings.general.track_mode == "releases":
        _git_network("fetch", "--tags", "origin")


def _latest_release_tag() -> str | None:
    """The newest dated release tag in the local tag namespace, or None.

    Selection rule (version, then HHMM) lives in `shared.release_tags` — the
    same rule `scripts/release_cut.py` uses, so the update track and the
    release cut can never disagree about what "the newest release" is.
    """
    from shared.release_tags import pick_latest_tag

    tag = pick_latest_tag(_git("tag", "--list").splitlines())
    return tag.name if tag is not None else None


def tracking_target_ref() -> str:
    """The ref `ava cluster update` converges to under the current track mode.

    `latest` -> `origin/<track_branch>` (the moving tip); `releases` -> the
    newest dated release tag (`vX.Y.Z-YYYYMMDD[HHMM]`), so merges to main stop
    deploying until a release cut. Requires `track_fetch()` to have run first;
    a `releases` cluster with no release tag yet raises — there is nothing to
    converge to, and guessing (falling back to main) would silently break the
    pinned-release guarantee.
    """
    if settings.general.track_mode == "releases":
        tag = _latest_release_tag()
        if tag is None:
            raise GitPullFailed(
                "track mode is 'releases' but no dated release tag "
                "(vX.Y.Z-YYYYMMDD) exists — cut one with "
                "`scripts/release_cut.py daily --push` first"
            )
        return tag
    return _tracking_remote_ref()


# Every git call carries a timeout: a hung git (dead network mid-fetch, a
# wedged filesystem) used to hang the whole detached rollout orchestration
# indefinitely — the cluster stayed paused with no verdict in the log.
# Local commands touch only the working tree / object store; network commands
# get a generous ceiling (corp egress can crawl) plus retries below.
#
# The timeout is applied by `shared.proc.run_bounded`, not by
# `subprocess.run(timeout=)`. That distinction is the whole invariant: plain
# `subprocess.run` kills only the process it spawned, which on Windows is
# Git-for-Windows' launcher stub rather than git — so the bound held on POSIX and
# was silently false on Windows, where the real git + its sh + its ssh survived
# every expiry. `run_bounded` kills the tree.
_GIT_LOCAL_TIMEOUT_S = 60.0
_GIT_NETWORK_TIMEOUT_S = 180.0
_PRE_UPDATE_DUMP_TIMEOUT_S = 20 * 60
_PRE_UPDATE_RESTORE_TIMEOUT_S = 60.0
_PG_RESTORE_TOC_ENTRY_RE = re.compile(r"^\d+;")

# Network git commands (fetch / pull) are idempotent and their dominant
# failure mode is a transient network blip — retry a few times with backoff.
# Local commands are deliberately NOT retried: a local failure (conflict,
# corrupt tree, dirty state) is a real problem retries would only mask.
# Non-retryable network failures (auth, removed remote) waste the retries and
# then fail loud — acceptable cost for not pattern-matching stderr.
_NETWORK_ATTEMPTS = 3
_NETWORK_RETRY_BACKOFF_S = 5.0

# A concurrent git process (another checkout/reset on the same tree) holds
# `.git/index.lock`. Git fails fast on the lock, which is fine — the failure is
# the guard — but a checkout that dies on the lock is NOT a checkout that did
# nothing: the working-tree writes happen outside the lock, so two checkouts
# racing the lock can still interleave file writes and leave a MIXED tree (the
# 2026-08-02 incident: rollout + watchdog self-heal raced, one updated the
# index, the other wrote files, and `ava start` imported a mixture of the two
# commits). So mutating git ops wait a bounded time for the lock before trying,
# and callers must verify the tree afterwards (`verify_tree_at`) rather than
# assume the checkout landed.
_INDEX_LOCK_WAIT_S = 30.0
_INDEX_LOCK_POLL_S = 2.0


class GitPullResult(NamedTuple):
    """`git_pull_main` return: old sha / new sha / commits behind. When
    `from_sha == to_sha`, behind is 0 (no new code)."""

    from_sha: str
    to_sha: str
    behind: int


class GitPullFailed(Exception):  # noqa: N818
    """Raised when `git rev-parse` / `pull` / `rev-list` exit with non-zero code.

    CLI-internal — `ava cluster update` runs git in a detached subprocess spawned by
    the gateway; after the process exits the stack trace ends up in the session
    session log, and the agent SDK path cannot catch it (after gateway
    detaches, stdout/stderr are DEVNULL and exceptions are not propagated).
    Inherits `Exception` rather than `ava.self.UpdateError` — avoids importing
    `ava.*` at the top of `cli/*` which would pull the entire SDK
    (LangGraph / anthropic / psycopg / redis / mcp) into the `ava status`
    startup path.
    """


class LocalAdminSchemaConnectionError(RuntimeError):
    """Recovery could not construct or open its local Postgres admin dial.

    This exception is deliberately narrower than failures raised after
    ``rollback_to`` begins: callers may conclude the schema is unchanged only
    for this pre-mutation boundary.
    """


def _wait_index_lock_free(timeout_s: float = _INDEX_LOCK_WAIT_S) -> None:
    """Wait up to `timeout_s` for a concurrent git process to release
    `.git/index.lock`, then raise `GitPullFailed` if it is still held.

    A mutating git op (checkout / pull / reset) must not start while another one
    is mid-flight on the same tree: the loser of the lock race still interleaves
    working-tree writes with the winner (the lock only serializes the index),
    which is exactly how a tree ends up half-one-commit-half-another. The wait is
    bounded — a wedged git process must not hang an update forever; beyond the
    bound the caller fails loudly instead of racing.
    """
    lock = _index_lock_path()
    deadline = time.monotonic() + timeout_s
    while lock.exists():
        if time.monotonic() >= deadline:
            raise GitPullFailed(
                f"another git process holds {lock} for >{timeout_s:.0f}s "
                "(a concurrent checkout/reset is in flight); refusing to run a "
                "second mutating git op on the same tree"
            )
        time.sleep(_INDEX_LOCK_POLL_S)


def _index_lock_path() -> Path:
    return _REPO_ROOT_FOR_GIT / ".git" / "index.lock"


def verify_tree_at(sha: str, *, context: str) -> None:
    """Fail-fast atomicity check after a checkout / reset / pull: HEAD must be
    exactly `sha` and every TRACKED path must match the index (untracked strays
    are not the poison — a missing/modified tracked file is, because the next
    `ava start` imports from the filesystem).

    A tree that fails either check is mixed or incomplete — the 2026-08-02
    rollout failure: two checkouts raced the index lock, interleaved their
    working-tree writes, and the resulting mixture imported as
    `ModuleNotFoundError: _health_preflight` / `cannot import name 'app_port'`.
    Callers must NOT proceed to `uv sync` / `ava start` on such a tree; raising
    here is what turns that silent corruption into a clean, recoverable failure.

    Raises:
        GitPullFailed: HEAD != sha, or `git status --porcelain --untracked-files=no`
            is non-empty. The message names the actual state so the operator can
            tell a moved-HEAD case from a dirty-tree case.
    """
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    problems: list[str] = []
    if head != sha:
        problems.append(f"HEAD is {head[:12]} (expected {sha[:12]})")
    if dirty:
        entries = dirty.splitlines()
        shown = ", ".join(e[:60] for e in entries[:3])
        more = f" (+{len(entries) - 3} more)" if len(entries) > 3 else ""
        problems.append(
            f"working tree is not clean ({len(entries)} tracked change(s): {shown}{more})"
        )
    if problems:
        raise GitPullFailed(f"{context}: checkout did not land atomically — " + "; ".join(problems))


def _git(*args: str, timeout_s: float = _GIT_LOCAL_TIMEOUT_S) -> str:
    """Run `git <args>` in repo root; return stdout stripped. Non-zero exit or
    a hang past `timeout_s` raises `GitPullFailed`."""
    # args are hardcoded CLI/SDK-internal subcommands, no untrusted input; list-form invocation insulates from shell injection
    try:
        result = run_bounded(
            ["git", *args],
            cwd=_REPO_ROOT_FOR_GIT,
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitPullFailed(
            f"`git {' '.join(args)}` timed out after {timeout_s:.0f}s "
            "(network black-hole or wedged filesystem; the process tree was killed)"
        ) from exc
    if result.returncode != 0:
        raise GitPullFailed(
            f"`git {' '.join(args)}` failed (rc={result.returncode}): "
            f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return result.stdout.strip()


def _git_network(*args: str) -> str:
    """`_git` for network-touching subcommands (fetch / pull): a longer timeout
    and up to `_NETWORK_ATTEMPTS` tries with backoff, each retry logged. Only
    idempotent network commands belong here — see the retry rationale above."""
    last: GitPullFailed | None = None
    for attempt in range(1, _NETWORK_ATTEMPTS + 1):
        try:
            return _git(*args, timeout_s=_GIT_NETWORK_TIMEOUT_S)
        except GitPullFailed as exc:
            last = exc
            if attempt == _NETWORK_ATTEMPTS:
                break
            print(
                f"  ⚠ `git {' '.join(args)}` attempt {attempt}/{_NETWORK_ATTEMPTS} failed "
                f"({exc}); retrying in {_NETWORK_RETRY_BACKOFF_S:.0f}s",
                file=sys.stderr,
            )
            time.sleep(_NETWORK_RETRY_BACKOFF_S)
    assert last is not None  # noqa: S101 — loop ran at least once
    raise GitPullFailed(
        f"`git {' '.join(args)}` failed after {_NETWORK_ATTEMPTS} attempts: {last}"
    ) from last


def git_pull_main() -> GitPullResult:
    """Advance HEAD to the track target — rev-parse HEAD, converge, rev-parse
    HEAD, rev-list count diff.

    `latest` mode does a plain `git pull origin <track_branch>`. `releases`
    mode fetches and force-checks-out the newest dated release tag (the same
    `checkout --force -B` semantics as the rollout's pinned-target checkout,
    so a prod source never ends up detached or on a feature branch).

    Raises:
        GitPullFailed: any git subcommand non-zero exit (dirty working tree / network / merge conflict).
    """
    branch = settings.general.track_branch
    from_sha = _git("rev-parse", "HEAD")
    if settings.general.track_mode == "releases":
        track_fetch()
        to_sha = _git("rev-parse", f"{tracking_target_ref()}^{{commit}}")
        if to_sha != from_sha:
            _wait_index_lock_free()
            _git("checkout", "--force", "-B", branch, to_sha)
    else:
        _wait_index_lock_free()
        _git_network("pull", "origin", branch)
        to_sha = _git("rev-parse", "HEAD")
    if to_sha != from_sha:
        # Same atomicity contract as git_checkout_sha: a raced pull can leave a
        # mixed tree, and the caller is about to rebuild/restart against it.
        verify_tree_at(to_sha, context="git_pull_main")
    behind = 0 if from_sha == to_sha else int(_git("rev-list", "--count", f"{from_sha}..{to_sha}"))
    return GitPullResult(from_sha=from_sha, to_sha=to_sha, behind=behind)


def git_head_sha() -> str:
    """Current `HEAD` sha — the last-known-good commit, snapshotted before a pull
    so a failed update can `git_reset_hard` back to it.

    Raises:
        GitPullFailed: `git rev-parse` non-zero exit.
    """
    return _git("rev-parse", "HEAD")


def git_reset_hard(sha: str) -> None:
    """`git reset --hard <sha>` in repo root — discard the just-pulled tree back to
    a known-good commit during update recovery.

    Raises:
        GitPullFailed: `git reset` non-zero exit, or the reset did not land
            atomically (HEAD != sha / dirty tree — a concurrent git op raced it).
    """
    _wait_index_lock_free()
    _git("reset", "--hard", sha)
    # Recovery's own atomicity check: this reset is what puts the host back on
    # last-known-good. If it raced another checkout and landed mixed, the
    # recovery `ava start` below would boot the mixture instead of known-good.
    verify_tree_at(sha, context="git_reset_hard")


def git_resolve_origin_main() -> str:
    """Fetch the track target then resolve its sha — the rollout's pinned
    target, resolved once on the gateway so every node checks out the same
    commit instead of each re-resolving a tip that moves mid-rollout.

    `latest` mode resolves `origin/<track_branch>`; `releases` mode resolves
    the newest dated release tag.

    Raises:
        GitPullFailed: a git subcommand non-zero exit, or a `releases` cluster
            with no release tag yet.
    """
    track_fetch()
    # Peel annotated tags (^{commit}): a release tag is created with `git tag -a`,
    # and a bare rev-parse of an annotated tag returns the tag OBJECT sha, which
    # is not a checkout-able commit and would poison the pin/behind bookkeeping.
    return _git("rev-parse", f"{tracking_target_ref()}^{{commit}}")


def git_checkout_sha(sha: str) -> str:
    """Force the working tree to exactly `sha` (the rollout's pinned target), from
    any starting branch or dirty state, and return the prior HEAD sha. `git fetch`
    first so `sha` is present.

    Commits reachable from the prior HEAD but not from `sha` (unpushed / diverged
    local work) are DISCARDED — logged here, recoverable via `git reflog` — because
    a prod source tracks the cluster's commit and is not a dev workspace. Resets
    branch `main` to `sha` (`checkout --force -B main`) so a node left on a feature
    branch lands back on main at the pinned commit.

    Raises:
        GitPullFailed: a git subcommand non-zero exit.
    """
    from_sha = _git("rev-parse", "HEAD")
    _git_network("fetch", "origin")
    # Count only commits that would be LOST — reachable from HEAD, not from the
    # target, AND not on any remote (`--not --remotes`). Without `--not --remotes`
    # this inflates when HEAD is merely ahead of the pinned sha on main (those
    # commits are still on origin); only genuinely-unpushed local work is at risk.
    discarded = (
        _git("rev-list", f"{sha}..{from_sha}", "--not", "--remotes") if from_sha != sha else ""
    )
    if discarded.strip():
        n = len(discarded.split())
        print(
            f"  ⚠ force-checkout to {sha[:7]} discards {n} unpushed local commit(s) on "
            f"{from_sha[:7]} (recoverable via `git reflog`)",
            file=sys.stderr,
        )
    _wait_index_lock_free()
    _git("checkout", "--force", "-B", settings.general.track_branch, sha)
    # Atomicity check: a checkout raced by a concurrent git process can land a
    # MIXED tree (index coherent, working tree a mixture of two commits) while
    # reporting success — the 2026-08-02 incident. Verify before the caller
    # syncs/starts against it.
    verify_tree_at(sha, context="git_checkout_sha")
    return from_sha


def apply_pending_migrations(*, release: ReleaseMigrationContext | None = None) -> list[str]:
    """Run all pending DB migrations; return the list of applied migration names
    (empty = no pending).

    Uses a fresh psycopg connection (does not reuse ava.DB), so migration
    transactions run on an independent connection and failure does not
    pollute the caller's session state (same pattern as the detached update
    orchestration, which runs `ava cluster update --local` out-of-process).

    Raises:
        MigrationFailed / SchemaVersionMismatch: specific subclasses raised by shared.migrations.
    """
    import shared.db
    from shared import migrations as _mig

    # direct=True: this wrapper is the `ava cluster update` / `ava cluster
    # rollback` migration path — the user ruling's ONE exemption (SESSION
    # advisory lock across the apply loop would be silently dropped by
    # transaction pooling; see cli/commands/migrations.py). unbounded=True:
    # migration DDL may exceed the 60s statement ceiling.
    with shared.db.connect(direct=True, unbounded=True) as conn:
        if release is None:
            return _mig.apply_pending_migrations(conn)
        return _mig.apply_pending_migrations(conn, release=release)


def _vet_rollout_target(target_sha: str) -> int | None:
    """validate-before-kill: vet the pinned target's migrations/ layout from git
    (no checkout, no DB) before the rollout pauses / stops anything.

    A duplicate / non-contiguous version only fails the migrate step at boot — after
    the stop — taking the whole cluster down (the 2026-06-17 dup-0049 outage). Returns
    1 (caller aborts the rollout with nothing paused) on a broken / unreadable target,
    else None to proceed.
    """
    from shared.migrations import MigrationLayoutError, validate_migrations_at_ref

    try:
        validate_migrations_at_ref(target_sha)
    except MigrationLayoutError as e:
        print(
            f"\n✗ refusing rollout: target {target_sha[:7]} has a broken migration "
            f"layout ({e}); aborting before any host is paused",
            file=sys.stderr,
        )
        return 1
    return None


def current_schema_state() -> set[str]:
    """The DB's applied-migration NAME set (the baseline sentinel plus any
    applied post-baseline deltas).

    Snapshotted right before `ava start` runs its migrations, capturing the
    last-known-good schema to roll back to if that start fails. Fresh connection
    (not ava.DB), same isolation rationale as apply_pending_migrations.
    """
    import shared.db
    from shared import migrations as _mig

    # direct=True: the snapshot is part of the admin update path (the set a
    # failed start rolls back to), and the pooler is mid-reload during
    # `ava cluster update`'s converge — read the schema from the real Postgres.
    with shared.db.connect(direct=True) as conn:
        return _mig.applied_migration_names(conn)


def _verify_snapshot_artifact(artifact: Path) -> None:
    """Verify an encrypted snapshot while keeping all error paths redacted."""
    try:
        _verify_snapshot_artifact_inner(artifact)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: verification failed "
            f"({type(exc).__name__})"
        ) from None


def _verify_snapshot_artifact_inner(artifact: Path) -> None:
    """Decrypt and list a snapshot without exposing its connection URL."""
    try:
        artifact_size = artifact.stat().st_size
    except OSError:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: cannot read artifact"
        ) from None
    if artifact_size <= 0:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: artifact is empty"
        )

    from services.backup import decrypt_artifact, gunzip_if_needed

    with tempfile.TemporaryDirectory(prefix="ava-pre-update-") as temporary_dir:
        temporary = Path(temporary_dir)
        dump = temporary / "snapshot.dump"
        try:
            decrypt_artifact(artifact, dump)
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decrypt failed "
                f"({type(exc).__name__})"
            ) from None
        try:
            dump_size = dump.stat().st_size
        except OSError:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: cannot read decrypted dump"
            ) from None
        if dump_size <= 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decrypted dump is empty"
            )
        try:
            # Fresh snapshots are raw custom dumps; a legacy artifact (written
            # before the 2026-08-27 double-gzip removal) still needs its gzip
            # layer removed before pg_restore can read it.
            gunzip_if_needed(dump, timeout_s=_PRE_UPDATE_RESTORE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: gunzip timed out after "
                f"{_PRE_UPDATE_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: gunzip failed "
                f"({type(exc).__name__})"
            ) from None
        try:
            dump_size = dump.stat().st_size
        except OSError:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: cannot read decompressed dump"
            ) from None
        if dump_size <= 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decompressed dump is empty"
            )
        try:
            listing = run_bounded(
                [str(shared.pg_tools.pg_tool("pg_restore")), "--list", str(dump)],
                timeout=_PRE_UPDATE_RESTORE_TIMEOUT_S,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"timed out after {_PRE_UPDATE_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"failed ({type(exc).__name__})"
            ) from None
        if listing.returncode != 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"exited {listing.returncode}"
            )
        if not any(_PG_RESTORE_TOC_ENTRY_RE.match(line) for line in listing.stdout.splitlines()):
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                "returned an empty table of contents"
            )


def snapshot_pre_update_data(target_sha: str) -> Path | None:
    """Verified pre-upgrade data snapshot, taken BEFORE anything is stopped.

    Returns the dump path when the update will apply migrations (the target's
    required migration set differs from the DB's current applied set), else None
    (a code-only update cannot damage data; the daily backup already covers it).

    The dump is a normal managed backup (`services.backup.run_backup`), so it
    lands in `<home>/backups/db/`; the `pre_update=True` marker names it
    `<db>-<ts>.pre-update.dump.enc`, and prune keeps the newest such snapshot
    in its own retention slot rather than consuming a daily-dump slot. The
    returned path is threaded through the
    recovery context so a failed rollout names the exact restore point, and is
    printed to the rollout output once verified so the snapshot is visible.

    Raises RuntimeError when the dump cannot be created OR verified: a rollout
    that will migrate with no recoverable data snapshot is exactly the P1 the
    audit names, so blowing up loudly — while the gateway is still up — is the
    right outcome (same philosophy as `_snapshot_known_good`'s schema read).
    """
    from cli.commands._cluster_rollback import _migration_set_at_commit

    if _migration_set_at_commit(target_sha) == current_schema_state():
        return None

    from services.backup import backup_lock, run_backup

    # Hold the same lock as the daily writer through the restore listing. A
    # verified dump must remain untouched until this function hands its path to
    # recovery; otherwise a scheduled writer can sweep its partial or replace
    # the same-second target between creation and verification.
    with backup_lock(timeout_s=_PRE_UPDATE_DUMP_TIMEOUT_S):
        try:
            dump_path = run_backup(
                timeout_s=_PRE_UPDATE_DUMP_TIMEOUT_S,
                pre_update=True,
                publish=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "could not create pre-update data snapshot: pg_dump timed out after "
                f"{_PRE_UPDATE_DUMP_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            # pg_dump's argv includes the direct DB URL. Do not stringify or
            # chain its failure into a rollout log.
            raise RuntimeError(
                f"could not create pre-update data snapshot: pg_dump failed ({type(exc).__name__})"
            ) from None

        _verify_snapshot_artifact(dump_path)
        # The verified path goes to the rollout output: without it, a snapshot
        # is invisible in the log (run_backup writes to its own daemon stream)
        # and an operator can mistake it for an unscheduled backup.
        print(f"→ pre-update data snapshot: {dump_path} (verified)")
        return dump_path


def snapshot_pre_activation_data(*, operation_id: str, db_url: str) -> Path:
    """Create the mandatory logical recovery floor before first PITR activation.

    Unlike the rollout helper above this is intentionally unconditional: the
    activation rollout cannot be protected by WAL archiving that is still off.
    """
    from services.backup import backup_lock, run_backup

    with backup_lock(timeout_s=_PRE_UPDATE_DUMP_TIMEOUT_S):
        try:
            dump_path = run_backup(
                timeout_s=_PRE_UPDATE_DUMP_TIMEOUT_S,
                db_url=db_url,
                pitr_activation=operation_id,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "could not create pre-activation data snapshot: pg_dump timed out after "
                f"{_PRE_UPDATE_DUMP_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                "could not create pre-activation data snapshot: "
                f"pg_dump failed ({type(exc).__name__})"
            ) from None
        _verify_snapshot_artifact(dump_path)
        print(f"→ pre-activation data snapshot: {dump_path} (verified)")
        return dump_path


def rollback_schema_to(keep: set[str], *, local_admin: bool = False) -> list[str]:
    """Roll the DB schema back to the applied set `keep` (the snapshot from
    current_schema_state); return the migration names rolled back (empty when the
    schema never advanced past `keep`). Fresh non-autocommit connection, same
    isolation rationale as apply_pending_migrations; the whole rollback runs in
    one transaction, so a failing down leaves the schema unchanged.

    `local_admin=True` is reserved for failed-update recovery on the gateway. A
    credential split may have changed the Postgres role password before its
    journal could be replayed into this surviving parent; recovery therefore
    dials this cluster's target database through the passwordless local admin
    socket instead of consulting the possibly-stale runtime URL credentials.

    Raises:
        RollbackBelowFloor: `keep` is below the squashed baseline — no down to
            reverse with; the caller treats this as not-auto-recoverable.
        MigrationFailed / MigrationLayoutError: from the down SQL.
    """
    import shared.db
    from shared import migrations as _mig

    if local_admin:
        import psycopg

        from cli.commands._cluster_instance import pg_admin_url
        from shared.cluster import _swap_db, get_record, record_postgres_port
        from shared.paths import ava_home

        def _connect_local_admin() -> psycopg.Connection:
            record = get_record(ava_home())
            if record is None:
                raise LocalAdminSchemaConnectionError(
                    "no cluster registry record for schema recovery"
                )
            db_name = urlsplit(settings.data_plane.db_url).path.lstrip("/")
            if not db_name:
                raise LocalAdminSchemaConnectionError(
                    "AVA_DB_URL carries no database name for schema recovery"
                )
            admin_url = _swap_db(pg_admin_url(record_postgres_port(record)), db_name)
            try:
                return psycopg.connect(
                    admin_url,
                    prepare_threshold=None,
                    **shared.db.PG_KEEPALIVE_KWARGS,
                )
            except Exception:
                # Keep the cause out of rollout logs: libpq failures can include
                # a connection URL. This type also proves rollback never began.
                raise LocalAdminSchemaConnectionError(
                    "could not open the local admin connection for schema recovery"
                ) from None

        conn = _connect_local_admin()
        with conn:
            return _mig.rollback_to(conn, keep)

    # direct=True + unbounded=True: rollback_to holds the same SESSION advisory
    # lock as the applier, and its down migrations are DDL that may exceed the
    # 60s statement ceiling (see cli/commands/migrations.py).
    with shared.db.connect(direct=True, unbounded=True) as conn:
        return _mig.rollback_to(conn, keep)
