"""Skills converge — sync repo + plugin skills into the single load dir.

`$AVA_HOME/skills/` is the only directory `ava.skills` scans (plus runtime
provider roots — see `ava/skills.py`). This module keeps it converged with the
three source kinds:

- repo skills:             `<repo>/ava_builtins/skills/<name>/`       -> `skills/<name>/`  (origin="repo")
- built-in plugin skills:  `<repo>/ava_builtins/plugins/<p>/skills/`  -> `skills/<p>/`     (origin="plugin")
- installed plugin skills: `$AVA_HOME/plugins/<p>/skills/` -> `skills/<p>/`   (origin="plugin")

`<repo>/.agents/skills/` (the kernel-contributor project skills) is NOT a
converge source: it reaches agents through the project-local mount instead
(issue #146).

Each synced top-level dir is tracked in the install registry
(`shared/install_registry.py`); the scanner loads only enabled entries. An
installed plugin's skills gate on the plugin's own registry entry — no
duplicate `type="skill"` row is created for it.

**Bootstrap-only for repo-native sources (R5 design, task #1013)**: converge
lands a repo-native package only when its load-dir copy is absent; it NEVER
updates an existing copy (updates are the explicit `ava skill update` command,
which owns the conflict/`--force` semantics). Installed-plugin skills keep the
older sync behavior (updated on source change, user edits protected).

Converge-managed copies are derived state: the registry's `content_hash`
records the tree hash converge / skill update last wrote, so a user edit to the
copy is detected and never clobbered (and never auto-removed). Sources vanish ->
untouched copies are removed; touched ones are kept with a warning.
User-origin entries (installed via `ava plugins install <git-url>` or
`ava skill register`) are never touched.

Runs as a converge step (`ava start` / `ava cluster update` / `ava converge`) and
directly after `ava plugins install`/`uninstall` so packages activate on the
next skill scan without a restart.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cli.commands._skill_package import contains_skill_md
from shared import install_registry, paths
from shared.cluster import is_default_home
from shared.install_registry import (
    IGNORED_NAMES,
    InstalledPackage,
    PackageOrigin,
    TrustTier,
    tree_hash,
)
from shared.skill_names import SkillIdentity, match_key

# A git worktree checkout's sources are branch work-in-progress. converge and
# `ava skill update` take their sources from the checkout the CLI runs from
# (`_repo_root()`), and in a worktree-heavy dev environment that assumption is
# false exactly when the CLI is run from a `.worktrees/...` or
# `.claude/worktrees/...` checkout while the resolved home is the default prod
# home — the worktree's branch content (possibly unmerged) then lands in
# `~/.ava/skills`, silently shadowing what main ships. Observed 2026-08-08: a
# R5 worktree's copy of `ava-serious-research` was synced into prod by a
# `skill update` run from that worktree (audit round 2, skills-plugins #3).
# Dev worktree clusters resolve their own home (`~/.ava-<dir>`), which is not
# the default home, so only the prod-home + worktree combination is refused.
_WORKTREE_MARKERS = (".claude/worktrees", "/.worktrees/")


def _is_worktree_checkout(repo: Path) -> bool:
    """Whether `repo` lives under a git worktree (`.worktrees/...` or
    `.claude/worktrees/...`) — same criterion `converge_host` uses to gate
    host-global steps."""
    resolved = str(repo.resolve())
    return any(marker in resolved for marker in _WORKTREE_MARKERS)


def assert_repo_source_bound(repo: Path, ava_home: Path) -> None:
    """Fail when a sync would write a *worktree* checkout's sources into the
    prod home's load dir.

    `AVA_CONVERGE_ALLOW_WORKTREE=1` opts out (a caller that means it).
    """
    if os.environ.get("AVA_CONVERGE_ALLOW_WORKTREE") == "1":
        return
    if is_default_home(ava_home) and _is_worktree_checkout(repo):
        raise RuntimeError(
            f"refusing to sync skills from worktree checkout {repo} into the prod "
            f"home {ava_home}: a worktree's sources are branch work-in-progress and "
            f"must not replace prod's load dir (run `ava` from the prod checkout, "
            f"or set AVA_CONVERGE_ALLOW_WORKTREE=1 to override)"
        )


def _copy_tree(src: Path, dest: Path) -> None:
    """Replace `dest` with a copy of `src`, minus `IGNORED_NAMES`.

    The replacement is staged: the new tree is fully copied to a dot-prefixed
    temp sibling first, marker-protected subtrees under `dest` (local adapters
    living inside a managed package) are moved into the staged copy, and only
    then is `dest` swapped out (dest -> trash, staged -> dest, trash removed).
    A crash mid-copy leaves `dest` untouched — never a half-written tree that a
    later converge misreads as a user edit (#974 failure mode). A crash after
    the swap leaves at worst a dot-prefixed temp/trash dir, which the untracked
    scan and the skills panel ignore; stale ones are swept at the next copy.
    """
    if not dest.exists():
        # Fresh copy is staged too: a partial copytree landing directly on
        # `dest` would block every later converge as an untracked shadow.
        staged = dest.parent / f".{dest.name}.new"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(src, staged, ignore=shutil.ignore_patterns(*IGNORED_NAMES))
        shutil.move(str(staged), str(dest))
        return
    staged = dest.parent / f".{dest.name}.new"
    trash = dest.parent / f".{dest.name}.trash"
    for leftover in (staged, trash):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(src, staged, ignore=shutil.ignore_patterns(*IGNORED_NAMES))
    # Marker-protected subtrees under `dest` ride into the staged copy (they
    # are load-dir-local content; the source does not carry them).
    for parts in install_registry.preserved_subpaths(dest):
        sub = dest.joinpath(*parts)
        if sub.exists():
            moved_to = staged.joinpath(*parts)
            moved_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub), str(moved_to))
    shutil.move(str(dest), str(trash))
    shutil.move(str(staged), str(dest))
    shutil.rmtree(trash)


@dataclass(frozen=True)
class _Source:
    """One sync unit: a source dir that lands at `skills/<name>/`."""

    name: str
    key: str  # SkillIdentity.from_dir(name).key — one skill = one match_key
    src: Path
    origin: PackageOrigin  # "repo" | "plugin" (never "user")
    trust: TrustTier
    """Content trust for what this source lands. `"builtin"` only when the tree
    came out of the Ava checkout — an *installed* plugin's skills converge with
    origin="plugin" too, but their content is third-party and was scanned at
    `ava plugins install` time, so they stay `"unreviewed"` here."""
    bootstrap_only: bool = False
    """True for repo-native sources (R5): converge lands the copy only when
    absent and never updates it — updates are the explicit `ava skill update`.
    False for installed-plugin skills, which keep the sync-on-change behavior."""


def _stamp_trust(entry: InstalledPackage, s: _Source) -> None:
    """Apply this source's content trust to its registry row.

    Converge owns the *builtin* stamp and nothing else: it marks repo-sourced
    content builtin, and takes the stamp back off if that name stops coming out
    of the checkout. A `reviewed` promotion is a human's statement about content
    they read (`ava skill trust`), so it survives every pass.
    """
    if s.trust == "builtin":
        entry.trust = "builtin"
    elif entry.trust == "builtin":
        entry.trust = "unreviewed"


def iter_sources(repo: Path) -> tuple[list[_Source], list[str]]:
    """Enumerate sync units in precedence order (repo skills, then built-in
    plugins, then installed plugins). A later source whose name is already
    taken is dropped and reported as a conflict."""
    sources: dict[str, _Source] = {}
    conflicts: list[str] = []

    def add(
        name: str, src: Path, origin: PackageOrigin, trust: TrustTier, *, bootstrap_only: bool
    ) -> None:
        # One skill = one match_key (design R2-B1): dash and underscore are
        # one name, so a repo `foo-bar/` and a plugin `foo_bar/` collide —
        # report it instead of letting both become registry rows (the
        # dual-row state the registry read now refuses).
        key = SkillIdentity.from_dir(name).key
        prev = next((s for s in sources.values() if s.key == key), None)
        if prev is not None:
            conflicts.append(f"{src} (name '{name}' already provided by {prev.src})")
            return
        sources[name] = _Source(name, key, src, origin, trust, bootstrap_only=bootstrap_only)

    repo_skills = repo / "ava_builtins" / "skills"
    if repo_skills.is_dir():
        for d in sorted(repo_skills.iterdir()):
            if d.is_dir() and d.name not in IGNORED_NAMES and contains_skill_md(d):
                add(d.name, d, "repo", "builtin", bootstrap_only=True)
    # The repo's `.agents/skills/` project skills (the kernel-contributor
    # family: ship-a-change, write-a-pr-description, …) are deliberately NOT a
    # converge source (issue #146 / decision 2026-08-20). They reach agents
    # only through the project-local mount — `project_skill_roots` in
    # `ava_builtins/plugins/ava_code/_walk.py` resolves `.agents/skills/` from
    # `ava.cwd` at scan time — so only an agent working inside the checkout
    # sees them, never a runtime agent via the fleet-wide load dir.
    builtin_then_installed: tuple[tuple[Path, TrustTier, bool], ...] = (
        (repo / "ava_builtins" / "plugins", "builtin", True),
        (paths.plugins_dir(), "unreviewed", False),
    )
    for plugins_root, trust, bootstrap_only in builtin_then_installed:
        if not plugins_root.is_dir():
            continue
        for p in sorted(plugins_root.iterdir()):
            skills_sub = p / "skills"
            if p.is_dir() and skills_sub.is_dir() and contains_skill_md(skills_sub):
                add(p.name, skills_sub, "plugin", trust, bootstrap_only=bootstrap_only)
    return list(sources.values()), conflicts


@dataclass
class SkillsConvergeResult:
    """What one converge pass did, for the caller to print / assert on."""

    copied: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # human-readable warnings: user-modified copies kept, name conflicts,
    # untracked dirs that will not load
    warnings: list[str] = field(default_factory=list)


def _adopt_copy(
    s: _Source,
    registry: install_registry.Registry,
    now: str,
    result: SkillsConvergeResult,
) -> None:
    """Adopt a registry-less, source-identical load-dir copy back under
    converge management (#974 self-heal: a wiped registry must not strand
    converge-managed copies forever)."""
    entry = InstalledPackage(name=s.name, type="skill", installed_at=now)
    registry.packages.append(entry)
    entry.origin = s.origin
    entry.origin_path = str(s.src)
    _stamp_trust(entry, s)
    entry.content_hash = tree_hash(s.src)
    entry.updated_at = now
    result.unchanged.append(s.name)


def _untracked_warning(s: _Source, dest: Path) -> str:
    """The registry-less differing-copy warning: the dir will not load and
    converge will not clobber it — register it to keep it."""
    return (
        f"'{s.name}': untracked dir {dest} shadows {s.src} (content differs); "
        f"not synced, not loaded (remove it, or `ava skill register {s.name}` to keep yours)"
    )


def _sync_one(
    s: _Source,
    skills_root: Path,
    registry: install_registry.Registry,
    now: str,
    result: SkillsConvergeResult,
) -> None:
    """Bring `skills_root/<s.name>` in line with `s.src` (copy / update /
    leave alone) and upsert its registry row. Appends its outcome to `result`."""
    dest = skills_root / s.name
    entry = next((p for p in registry.packages if match_key(p.name) == s.key), None)
    # An installed plugin's own skills dir is fair game even while its
    # registry row still says origin="user" (the state right after
    # `ava plugins install`, before the first converge).
    own_plugin_skills = (
        entry is not None
        and entry.type == "plugin"
        and s.origin == "plugin"
        and s.src == paths.plugins_dir() / entry.name / "skills"
    )
    if entry is not None and entry.origin == "user" and not own_plugin_skills:
        # A user-installed package holds this name; its content is not
        # derived state, so the repo/plugin source loses. (Design calls
        # for repo-wins, but overwriting would destroy user content.)
        result.warnings.append(
            f"'{s.name}': user-installed package shadows {s.src}; source not synced"
        )
        return
    src_hash = tree_hash(s.src)
    skip = install_registry.preserved_subpaths(dest)
    if s.bootstrap_only and dest.exists():
        # Repo-native sources are bootstrap-only (R5): an existing copy is
        # never touched here — updates are the explicit `ava skill update`
        # (which owns conflict detection + --force). The one exception is the
        # lost-registry-row self-heal (#974): a source-identical copy is
        # adopted back; a differing registry-less copy stays an untracked
        # warning (it may be a deliberate edit — adopting it would let a later
        # update overwrite it).
        if entry is None:
            if src_hash == tree_hash(dest, skip_subtrees=skip):
                _adopt_copy(s, registry, now, result)
            else:
                result.warnings.append(_untracked_warning(s, dest))
        else:
            # Re-anchor a stale recorded source on the no-op pass (#15): a row
            # written from a deleted worktree keeps pointing at it otherwise.
            if entry.origin_path != str(s.src):
                entry.origin_path, entry.updated_at = str(s.src), now
            result.unchanged.append(s.name)
        return
    if not dest.exists():
        _copy_tree(s.src, dest)
        if entry is None:
            entry = InstalledPackage(name=s.name, type="skill", installed_at=now)
            registry.packages.append(entry)
        entry.origin = s.origin
        entry.origin_path = str(s.src)
        _stamp_trust(entry, s)
        entry.content_hash = src_hash
        entry.updated_at = now
        if entry.installed_at is None:
            entry.installed_at = now
        result.copied.append(s.name)
        return
    dest_hash = tree_hash(dest, skip_subtrees=skip)
    if entry is None:
        if src_hash == dest_hash:
            # The registry row was lost (a wiped / rewritten installed.json),
            # but this copy is byte-identical to the source — adopt it back
            # under converge management instead of stranding it untracked
            # forever. A content-DIFFERING copy stays a warning below: it may
            # be a user's deliberate edit, and adopting it would let a later
            # converge pass overwrite that edit.
            _adopt_copy(s, registry, now, result)
            return
        # Stray copy squatting on a managed name: untracked dirs never
        # load, so just surface it — do not clobber or adopt.
        result.warnings.append(_untracked_warning(s, dest))
        return
    if entry.content_hash is not None and dest_hash != entry.content_hash:
        result.warnings.append(
            f"'{s.name}': {dest} was modified locally; not overwritten "
            f"(remove the dir and re-run converge to restore from {s.src})"
        )
        return
    entry.origin = s.origin
    entry.origin_path = str(s.src)
    _stamp_trust(entry, s)
    if src_hash == dest_hash:
        if entry.content_hash is None:  # tracked pre-hash copy — adopt it
            entry.content_hash = src_hash
            entry.updated_at = now
        result.unchanged.append(s.name)
        return
    _copy_tree(s.src, dest)
    entry.content_hash = src_hash
    entry.updated_at = now
    result.updated.append(s.name)


def _cleanup_gone_sources(
    managed: set[str],
    skills_root: Path,
    registry: install_registry.Registry,
    result: SkillsConvergeResult,
) -> None:
    """Handle converge-managed entries whose source vanished. Untouched copies
    are pure derived state -> removed; user-edited copies are kept (loudly).
    Only `type="skill"` rows are deregistered — a `type="plugin"` row is owned
    by `ava plugins uninstall`."""
    for entry in list(registry.packages):
        if entry.origin == "user" or match_key(entry.name) in managed:
            continue
        dest = skills_root / entry.name
        if dest.exists():
            if (
                entry.content_hash is not None
                and tree_hash(dest, skip_subtrees=install_registry.preserved_subpaths(dest))
                != entry.content_hash
            ):
                result.warnings.append(
                    f"'{entry.name}': source {entry.origin_path} is gone but {dest} was "
                    f"modified locally; kept (remove it or `ava skill register {entry.name}`)"
                )
                continue
            if any(
                dest.joinpath(*parts).exists()
                for parts in install_registry.preserved_subpaths(dest)
            ):
                result.warnings.append(
                    f"'{entry.name}': source {entry.origin_path} is gone but {dest} holds "
                    f"preserved local content; kept (remove it only if that content is gone)"
                )
                continue
            shutil.rmtree(dest)
            result.removed.append(entry.name)
        if entry.type == "skill":
            registry.packages.remove(entry)
        else:
            # e.g. an installed plugin whose skills/ vanished: hand its row
            # back to install management so converge stops tracking it.
            entry.origin = "user"
            entry.origin_path = None
            entry.content_hash = None
            if entry.trust == "builtin":
                # Nothing in the checkout backs this content any more, so the
                # builtin stamp no longer describes it.
                entry.trust = "unreviewed"


def converge_skills(repo: Path, ava_home: Path) -> SkillsConvergeResult:
    """Sync all skill sources into `<ava_home>/skills/` and update the install
    registry. Idempotent; never touches user-origin entries or user-edited
    copies. See the module docstring for the full rules."""
    assert_repo_source_bound(repo, ava_home)
    skills_root = ava_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    result = SkillsConvergeResult()

    with install_registry.mutate() as registry:
        sources, conflicts = iter_sources(repo)
        for c in conflicts:
            result.warnings.append(f"skipped {c}")
        for s in sources:
            _sync_one(s, skills_root, registry, now, result)
        _cleanup_gone_sources({s.key for s in sources}, skills_root, registry, result)

    tracked = {p.name for p in registry.packages}
    untracked = sorted(
        d.name
        for d in skills_root.iterdir()
        if d.is_dir()
        and d.name not in IGNORED_NAMES
        and not d.name.startswith(".")  # converge's own temp/trash staging dirs
        and d.name not in tracked
    )
    for name in untracked:
        result.warnings.append(
            f"'{name}': untracked dir in {skills_root} — not loaded "
            f"(`ava skill register {name}` to load it)"
        )
    return result
