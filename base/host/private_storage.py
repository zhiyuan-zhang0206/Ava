"""Owner-only local storage helpers for secrets and uploaded user files."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from base.log import logger


def _private_path_error(path: Path, reason: str) -> RuntimeError:
    return RuntimeError(f"private storage path {path} {reason}")


def _is_foreign_owned(current: os.stat_result) -> bool:
    """Return whether a path is not owned by this process on POSIX.

    chmod honors the owner (uid) alone, not the group — a uid-ours file
    with a foreign gid (chgrp'd leftover) is still repairable, and skipping
    it would strand a 0o644 file as group-readable.
    """
    return os.name != "nt" and current.st_uid != os.geteuid()


def _dir_refusal(current: os.stat_result) -> str | None:
    """Why a private-directory candidate is unusable, or None.

    One definition for the three readers of the rule: the writer
    (`ensure_private_dir`), the converge walk, and the read-only predicates the
    update leg's pre-stop gate consults — so a repair and a pre-flight cannot
    disagree about what is broken.
    """
    if stat.S_ISLNK(current.st_mode):
        return "is a symlink"
    if not stat.S_ISDIR(current.st_mode):
        return "is not a directory"
    if _is_foreign_owned(current):
        return "is not owned by the current user"
    return None


def _file_refusal(current: os.stat_result) -> str | None:
    """Why a private-file candidate is unusable, or None (see `_dir_refusal`)."""
    if stat.S_ISLNK(current.st_mode):
        return "is a symlink"
    if not stat.S_ISREG(current.st_mode):
        return "is not a regular file"
    return None


def ensure_private_dir(path: Path) -> Path:
    """Create `path` if needed, then require an owner-only real directory."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        refusal = _dir_refusal(before)
        if refusal is not None:
            raise _private_path_error(path, refusal)

    current = path.lstat()
    refusal = _dir_refusal(current)
    if refusal is not None:
        raise _private_path_error(path, refusal)
    path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> None:
    """Repair an existing regular file to owner-only mode; ignore a missing file."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    refusal = _file_refusal(current)
    if refusal is not None:
        raise _private_path_error(path, refusal)
    path.chmod(0o700 if current.st_mode & stat.S_IXUSR else 0o600)


def private_file_problem(path: Path) -> str | None:
    """Read-only twin of `ensure_private_file`'s refusal: why it would raise, or None.

    Used by the update leg's pre-stop gate to see — without the chmod repair —
    the failure `ava start` would hit later: a marker or secrets file the
    converge writer refuses because a symlink or a non-regular node sits at its
    path. A missing file is not a problem (the writer creates it).
    """
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    return _file_refusal(current)


# A `node_modules` directory anywhere in a private tree is third-party
# package content: nothing under it is a secret this converge must repair, and
# a pnpm store buries tens of thousands of symlinks there — descending is pure
# per-converge cost plus per-node log noise (2026-10-03 alert triage).
_NODE_MODULES_DIRNAME = "node_modules"

# Skipped-path examples carried by the one-summary-per-tree warning: enough to
# name what the flood is, small enough to stay readable (the full count is in
# the same line).
_SUMMARY_EXAMPLE_LIMIT = 3


@dataclass(slots=True)
class _SkipReport:
    """Per-tree tally of the nodes converge skipped, for the summary warning."""

    symlinks: list[Path] = field(default_factory=list)
    foreign_owned: list[Path] = field(default_factory=list)
    non_regular: list[Path] = field(default_factory=list)
    node_modules_dirs: int = 0

    @property
    def total(self) -> int:
        return len(self.symlinks) + len(self.foreign_owned) + len(self.non_regular)

    def examples(self) -> str:
        """The first few skipped paths, each tagged with its reason."""
        tagged = (
            [(path, "symlink") for path in self.symlinks]
            + [(path, "foreign-owned") for path in self.foreign_owned]
            + [(path, "non-regular") for path in self.non_regular]
        )
        shown = "; ".join(f"{path} ({reason})" for path, reason in tagged[:_SUMMARY_EXAMPLE_LIMIT])
        return f"{shown}; …" if len(tagged) > _SUMMARY_EXAMPLE_LIMIT else shown


def converge_private_tree(path: Path) -> Path:
    """Recursively converge a private directory tree to owner-only modes.

    A unit's logs, workspaces, and memory checkout can predate the private
    storage convention. Converge owns their durable permission repair, but it
    must never follow a symlink out of the tree while doing so, and a node it
    cannot repair — a symlink, a foreign owner, a socket or FIFO — is skipped:
    one unexpected node must not abort the run, because the abort takes the
    updater (and with it the host) down (2026-09-12).

    A skipped node keeps a per-node DEBUG line; the run reports ONE summary
    WARNING per tree (counts plus a few examples). A pnpm workspace tree holds
    tens of thousands of expected symlinks, so per-node warnings drowned every
    other signal (18,516 warning rows in one deploy window — 2026-10-03
    triage). `node_modules` subtrees are not descended at all; the directory
    itself is still converged like any other child.
    """
    skips = _SkipReport()
    _converge_tree(path, skips)
    if skips.total:
        logger.warning(
            "private storage convergence skipped {n} node(s) under {path} "
            "(symlink={symlinks}, foreign_owned={foreign_owned}, "
            "non_regular={non_regular}; node_modules dirs not descended={node_modules}) "
            "— first: {examples}",
            n=skips.total,
            path=path,
            symlinks=len(skips.symlinks),
            foreign_owned=len(skips.foreign_owned),
            non_regular=len(skips.non_regular),
            node_modules=skips.node_modules_dirs,
            examples=skips.examples(),
        )
    return path


def _converge_tree(path: Path, skips: _SkipReport) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and _is_foreign_owned(current):
        # A directory owned by another account can never be chmod'd by
        # converge — warn and leave it (and its subtree) alone instead of
        # aborting the whole converge run (wsl 2026-09-02 boot loop).
        logger.warning("private storage convergence skipped foreign-owned path {path}", path=path)
        return
    ensure_private_dir(path)
    for child in path.iterdir():
        current = child.lstat()
        if stat.S_ISLNK(current.st_mode):
            # Workspace trees can link tooling outside AVA_HOME; private-tree
            # convergence must not recurse into or alter those targets.
            logger.debug("private storage convergence skipped symlink {path}", path=child)
            skips.symlinks.append(child)
            continue
        if _is_foreign_owned(current):
            logger.debug(
                "private storage convergence skipped foreign-owned path {path}", path=child
            )
            skips.foreign_owned.append(child)
            continue
        if stat.S_ISDIR(current.st_mode):
            if child.name == _NODE_MODULES_DIRNAME:
                ensure_private_dir(child)
                skips.node_modules_dirs += 1
                continue
            _converge_tree(child, skips)
            continue
        if not stat.S_ISREG(current.st_mode):
            # Unix sockets and FIFOs are live-service plumbing, not storage:
            # there is no permission converge could repair, and raising over a
            # dead workspace socket aborted the whole converge — and with it
            # the updater and the host (macmini 2026-09-12). Skip like symlinks.
            logger.debug("private storage convergence skipped non-regular file {path}", path=child)
            skips.non_regular.append(child)
            continue
        ensure_private_file(child)


def private_tree_root_problem(path: Path) -> str | None:
    """Read-only: why `converge_private_tree(path)` would ABORT on this root, or None.

    The root half of the pre-stop check, without the repair: a missing root is
    fine (converge creates it), and a foreign-owned root is fine here too —
    converge warns and skips it rather than aborting. What remains is exactly
    what `ensure_private_dir` refuses: a symlink, or a path that is not a
    directory.
    """
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    if _is_foreign_owned(current):
        return None
    return _dir_refusal(current)


def scan_non_regular_nodes(root: Path) -> list[Path]:
    """Read-only: the nodes `converge_private_tree(root)` would skip as non-regular.

    Sockets, FIFOs, and device nodes are live-service plumbing, not storage:
    converge has no permission repair for them and skips them (macmini
    2026-09-12). The walk mirrors that traversal — symlinks, foreign-owned
    paths and `node_modules` subtrees are not descended, exactly as converge
    does not follow or descend them — so the pre-stop report and the converge
    log cannot disagree about what will be skipped. A missing or unsuitable
    root yields [] (converge handles the root itself; `private_tree_root_problem`
    is its predicate). Read-only; the returned paths are in traversal order.
    """
    out: list[Path] = []
    try:
        current = root.lstat()
    except FileNotFoundError:
        return out
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        return out
    if _is_foreign_owned(current):
        return out
    for child in root.iterdir():
        child_stat = child.lstat()
        if stat.S_ISLNK(child_stat.st_mode) or _is_foreign_owned(child_stat):
            continue
        if stat.S_ISDIR(child_stat.st_mode):
            if child.name == _NODE_MODULES_DIRNAME:
                continue
            out += scan_non_regular_nodes(child)
        elif not stat.S_ISREG(child_stat.st_mode):
            out.append(child)
    return out


def create_private_bytes(path: Path, data: bytes) -> None:
    """Publish owner-only `data` at `path` complete or not at all; never overwrite.

    Two racing creators cannot both win: the loser gets `FileExistsError` and
    reads the winner's file instead.
    """
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace `path` with owner-only `data` in its private directory."""
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)  # noqa: PTH105 — explicit atomic replacement primitive
        ensure_private_file(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)
