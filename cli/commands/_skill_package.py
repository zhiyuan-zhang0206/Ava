"""Find and install Agent Skills standard skill packages from a source tree.

The unit of installation is one **skill package**: a directory holding a
SKILL.md, installed verbatim (sub-skills, `scripts/`, `references/`, `assets/`
and anything else it carries come along) into `$AVA_HOME/skills/<name>/`, where
the skill scanner mounts it.

A source tree names its skills in one of three shapes, tried in this order:

1. **bare skill** — SKILL.md at the tree root; the tree *is* one package.
2. **collection root** — a `skills/`, `.claude/skills/`, `.agents/skills/`
   or `.ava/skills/` directory whose children are skill packages. This is how the ecosystem
   ships more than one skill in a repo, and it is why an unmodified Claude
   Code skills tree installs as-is. The first of the three that exists and
   holds a skill wins (a repo carrying both usually carries the same skills
   twice).
3. **bare collection** — the tree root's own visible children are skill
   packages (a repo that is just a pile of skill folders).

Nothing else is guessed: a tree matching none of the three is an error, not a
no-op install. Malformed SKILL.md is an error too — this is a user-initiated
install, so it fails here loudly rather than degrading into a skill the runtime
scanner would skip-warn past forever.

Every package installed through here is third-party content by construction
(repo- and plugin-owned skills reach the load dir via converge, not this path),
so it is put through `shared.skill_scan` before the first byte is copied. A
critical finding refuses the whole install; `accept_risk=True` is the human
override, and it records what was accepted on the registry row.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shared import skill_scan
from shared.install_registry import IGNORED_NAMES, tree_hash

_COLLECTION_ROOTS = ("skills", ".claude/skills", ".agents/skills", ".ava/skills")


class SkillPackageError(ValueError):
    """The source tree holds no installable skill package, one of its SKILL.md
    files does not parse, or a package is already installed."""


class SkillScanRefused(SkillPackageError):  # noqa: N818 — reads as the outcome, not an error kind
    """A package carries critical scan findings and no override was given. Its
    `report` is the rendered finding list; nothing was written to disk."""

    def __init__(self, report: str) -> None:
        super().__init__(report)
        self.report = report


@dataclass(frozen=True)
class SkillPackage:
    """One installable skill directory. `root` holds the SKILL.md; `name` is its
    frontmatter name, which is also the directory name it installs under."""

    root: Path
    name: str


def contains_skill_md(d: Path) -> bool:
    """True when `d` holds a SKILL.md at any depth — the "skill-bearing tree"
    predicate skills converge uses to decide whether a source dir is worth
    syncing.

    Deliberately broader than `_package_at`'s root-SKILL.md rule, and the two
    are not interchangeable: the scanner mounts SKILL.md files at any depth
    (namespaced by folder path), so a plugin whose `skills/` root has only
    nested skills (`ava_code/pr/SKILL.md`, no `skills/SKILL.md`) still syncs.
    Install is the inverse — a *package* needs a root SKILL.md because its
    frontmatter name is the install identity.
    """
    return any(d.rglob("SKILL.md"))


def _package_at(d: Path) -> SkillPackage | None:
    """`d` as a skill package, or None when it holds no SKILL.md.

    Raises:
        SkillPackageError: `d/SKILL.md` exists but does not parse / is missing a
            required field.
    """
    from shared.skill_index import SkillFormatError
    from shared.skill_index import parse_skill_frontmatter as _parse_frontmatter

    skill_md = d / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        fields, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except SkillFormatError as e:
        raise SkillPackageError(f"bad {skill_md}: {e}") from e
    return SkillPackage(root=d, name=fields["name"])


def _packages_under(d: Path) -> list[SkillPackage]:
    """Every visible child directory of `d` that is a skill package, name-sorted.
    Hidden dirs are skipped so a repo's `.git` / `.github` never reads as one."""
    if not d.is_dir():
        return []
    found: list[SkillPackage] = []
    for child in sorted(d.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        pkg = _package_at(child)
        if pkg is not None:
            found.append(pkg)
    return found


def discover(root: Path) -> list[SkillPackage]:
    """Every skill package `root` offers, by the three layouts in the module
    docstring.

    Raises:
        SkillPackageError: no layout matched, or a SKILL.md is malformed.
    """
    bare = _package_at(root)
    if bare is not None:
        return [bare]
    for rel in _COLLECTION_ROOTS:
        found = _packages_under(root / rel)
        if found:
            return found
    found = _packages_under(root)
    if found:
        return found
    raise SkillPackageError(
        f"no skill found in {root}: expected a SKILL.md at the root, a "
        f"{' / '.join(_COLLECTION_ROOTS)} directory of skills, or skill "
        "directories at the root."
    )


def register_installed(
    name: str,
    pkg_type: str,
    url: str | None,
    path: str | None,
    ref: str | None,
    *,
    accepted_findings: list[str] | None = None,
    content_hash: str | None = None,
) -> None:
    """Record a freshly-installed package in the install registry (origin='user',
    so converge leaves it alone).

    Trust is always `"unreviewed"` here: this is third-party content, and a
    clean scan does not stand in for a human having read it. Promotion is the
    separate, explicit `ava skill trust <name>`.

    `content_hash` / `installed_hash` record the tree hash of what was just
    written (R5): the later `upgrade` path compares the on-disk tree against
    `installed_hash` to detect local edits before overwriting. (`content_hash`
    is written too so converge's copy-change detection agrees on first sight;
    on installed-plugin rows converge then re-owns `content_hash` for the
    load-dir skills copy.)
    """
    from shared import install_registry

    now = datetime.now(UTC).isoformat(timespec="seconds")
    install_registry.register(
        install_registry.InstalledPackage(
            name=name,
            type=pkg_type,  # type: ignore[arg-type]  # callers pass a PackageType literal
            source=url,
            path=path,
            ref=ref,
            enabled=True,
            installed_at=now,
            updated_at=now,
            trust="unreviewed",
            scanned_at=now,
            accepted_findings=accepted_findings or [],
            content_hash=content_hash,
            installed_hash=content_hash,
        )
    )


def scan_report(root: Path, name: str, *, accept_risk: bool) -> tuple[str, list[str]]:
    """Scan the package tree at `root`; return its rendered report and the rule
    ids of any critical findings that were overridden.

    Raises:
        SkillScanRefused: critical findings and `accept_risk` is False.
    """
    findings = skill_scan.scan_package(root)
    report = skill_scan.render(findings, package=name)
    critical = skill_scan.criticals(findings)
    if critical and not accept_risk:
        raise SkillScanRefused(report)
    return report, skill_scan.rule_ids(critical)


def _register_in_cluster(
    packages: list[SkillPackage], *, source: str | None, ref: str | None
) -> None:
    """Write every package to the cluster extension registry, or raise.

    Deliberately NOT best-effort. The cluster row is the authority the whole
    ownership model rests on (`decisions/2026-08-21-extension-ownership-three-tiers.md`),
    so an install that cannot reach the cluster must not quietly become a
    machine-local one — that is the drift, not a degraded mode of avoiding it.
    The operator's fix is to bring the cluster up and re-run, and the error says
    so.

    A LOCAL-PATH source is recorded as `local:<machine>`, never as the path
    itself. The row is cluster-wide, and `/Users/someone/skills/foo` means
    nothing on any other machine — storing it would put a machine-local fact in
    a cluster table, which is the drift class this whole model removes. The
    classifier is `_pkg_source.looks_like_local_path`, the same one the install
    itself uses to decide whether to clone, so "is this local" cannot be
    answered two ways. The design's `source` vocabulary is exactly
    `'repo' | <git URL> | 'local:<machine>'`.

    `path` is not carried: it locates the package WITHIN a source repo, which is
    a fact about how this machine fetched it, and the cluster row is keyed by
    name and addressed by content.
    """
    from shared import db, extension_registry
    from shared.machine import machine_name

    from ._pkg_source import looks_like_local_path

    origin = (
        f"local:{machine_name()}" if source is None or looks_like_local_path(source) else source
    )
    local = origin.startswith("local:")
    # A git ref pins a remote source; it says nothing about a local directory.
    ref = None if local else ref
    pool = db.pool()
    for pkg in packages:
        extension_registry.register_tree(
            pool,
            root=pkg.root,
            name=pkg.name,
            kind="skill",
            source=origin,
            source_ref=ref,
        )


def install(
    packages: list[SkillPackage],
    *,
    source: str | None,
    path: str | None,
    ref: str | None,
    accept_risk: bool = False,
) -> list[tuple[Path, str]]:
    """Copy every package into the skills load dir and register it; return the
    installed `(destination, scan report)` pairs in the order given.

    Both gates — destination collisions and the security scan — run over *all*
    packages before the first copy, so a bad apple in a multi-skill source
    leaves the load dir untouched rather than half-populated.

    Raises:
        SkillPackageError: a destination is already occupied.
        SkillScanRefused: a package carries critical findings and `accept_risk`
            is False.
    """
    from shared import paths

    dest_root = paths.skills_dir()
    clashes = [pkg.name for pkg in packages if (dest_root / pkg.name).exists()]
    if clashes:
        raise SkillPackageError(
            f"already installed: {', '.join(clashes)} — "
            "`ava plugins uninstall <name>` first, or `ava plugins upgrade <name>` to re-fetch."
        )
    scanned = [scan_report(pkg.root, pkg.name, accept_risk=accept_risk) for pkg in packages]
    # The CLUSTER row lands before anything touches this machine's disk, and the
    # whole batch lands before the first copy. Ordering is the point: a failure
    # here leaves nothing installed anywhere, whereas registering after the copy
    # would leave a machine holding a skill the cluster has never heard of —
    # exactly the drift the registry exists to delete. The reverse gap (a row
    # whose content is not on this machine yet) is not a gap at all: it is what
    # materialization is for, on every machine including this one.
    _register_in_cluster(packages, source=source, ref=ref)
    dest_root.mkdir(parents=True, exist_ok=True)
    installed: list[tuple[Path, str]] = []
    for pkg, (report, accepted) in zip(packages, scanned, strict=True):
        dest = dest_root / pkg.name
        shutil.copytree(
            pkg.root,
            dest,
            ignore=shutil.ignore_patterns(*IGNORED_NAMES),
        )
        register_installed(
            pkg.name,
            "skill",
            source,
            path,
            ref,
            accepted_findings=accepted,
            content_hash=tree_hash(dest),
        )
        installed.append((dest, report))
    return installed
