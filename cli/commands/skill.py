"""`ava skill` subcommands — install into, and toggle entries in, the skills load dir.

installed (the dir exists under `$AVA_HOME/skills/`) and enabled (the skill
scanner loads it) are orthogonal: the scanner loads only directories the
install registry tracks as enabled, so a hand-copied dir is invisible until
registered and a disabled one stays on disk but out of every agent's tree.

- `install <src>`    — install Agent Skills standard skill package(s) from a git
  URL or a local directory (layout detection in `_skill_package.py`).
- `enable <name>` / `disable <name>` — flip the registry toggle (any tracked
  package: converged repo/plugin skills, user installs, plugin namespaces).
- `register <name>`  — start tracking a dir already under `$AVA_HOME/skills/`
  as a user package (origin="user", so converge never touches it).
- `scan <name-or-path>` — re-run the supply-chain scan and print the report.
- `trust <name>`     — record that a human read this package (tier "reviewed").

Everything arriving through `install` / `register` is third-party content, so
it goes through `shared.skill_scan` first: a critical finding refuses the
install outright and `--accept-risk` is the recorded override. Nothing here
promotes a package's trust tier on its own — a clean scan means "no rule
matched", which is why `trust` is a separate verb a person runs.

Removing a package is `ava plugins uninstall`; `ava plugins install` is the
sibling entry point for a Claude Code *plugin* bundle (agents / commands /
`.mcp.json`, of which skills are one part). The converge that populates the
load dir from repo + plugin sources is `cli/commands/_converge_skills.py`.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cli.commands._converge_skills import _Source
from shared import install_registry as reg
from shared.config import settings

_REFUSAL_ADVICE = (
    "  Refusing to install. Read the package yourself — these patterns are how a\n"
    "  malicious skill reaches your credentials, and the report above says where.\n"
    "  If you have read it and still want it, re-run with --accept-risk (the\n"
    "  accepted rule ids are recorded, and the package stays trust=unreviewed)."
)


def cmd_skill_install(
    source: str, ref: str | None, path: str | None, *, accept_risk: bool = False
) -> int:
    """`ava skill install <git-url-or-local-path> [--ref REF] [--path SUBDIR]
    [--accept-risk]` — install Agent Skills standard skill package(s) into
    `$AVA_HOME/skills/`.

    The source is used as published: a bare skill folder, a repo of skills under
    `skills/` or `.claude/skills/`, or a pile of skill folders all install
    unmodified. A local path is read in place (never moved), so a skill tree
    that lives in a checkout can be installed straight from it.

    Every package is scanned first (`shared.skill_scan`). A critical finding
    aborts the whole install with the report; `--accept-risk` overrides it and
    records which rules were waived.
    """
    from ._pkg_source import SourcePathNotFoundError, acquire_source, cleanup_temp
    from ._skill_package import SkillPackageError, SkillScanRefused, discover, install

    try:
        acquired = acquire_source(source, ref)
    except (subprocess.CalledProcessError, SourcePathNotFoundError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"[ava skill install] {detail}", file=sys.stderr)
        return 1
    try:
        root = acquired.root / path if path else acquired.root
        if not root.is_dir():
            print(f"[ava skill install] path '{path}' not found in source.", file=sys.stderr)
            return 1
        from ._manifest_gate import gate_refuses

        if gate_refuses(root, command="skill install"):
            return 1

        try:
            packages = discover(root)
            results = install(packages, source=source, path=path, ref=ref, accept_risk=accept_risk)
        except SkillScanRefused as e:
            print("[ava skill install] security scan found critical patterns:", file=sys.stderr)
            print(e.report, file=sys.stderr)
            print(_REFUSAL_ADVICE, file=sys.stderr)
            return 1
        except SkillPackageError as e:
            print(f"[ava skill install] {e}", file=sys.stderr)
            return 1
        for pkg, (dest, report) in zip(packages, results, strict=True):
            print(f"[ava skill install] installed skill '{pkg.name}' -> {dest}")
            print(report)
        print("  trust=unreviewed — `ava skill trust <name>` once a person has read it.")
        print("  active on the next skill scan; no restart needed.")
        return 0
    finally:
        cleanup_temp(acquired.cloned)


def cmd_skill_scan(target: str) -> int:
    """`ava skill scan <name-or-path>` — print the supply-chain scan report for
    an installed package or any directory on disk.

    The read-it-before-you-trust-it companion to `ava skill trust`, and the way
    to re-scan an installed package after the rule table grows. Exit code 2 on
    critical findings, so a caller can gate on it; 0 when only notices remain.
    """
    from shared import install_registry, paths, skill_scan

    root = Path(target).expanduser()
    if not root.is_dir():
        root = paths.skills_dir() / target
    if not root.is_dir():
        print(
            f"[ava skill scan] no directory at '{target}' nor {paths.skills_dir() / target}.",
            file=sys.stderr,
        )
        return 1
    findings = skill_scan.scan_package(root)
    entry = install_registry.get(root.name)
    tier = entry.trust if entry is not None else "untracked"
    print(f"[ava skill scan] {root} (trust={tier})")
    print(skill_scan.render(findings, package=root.name))
    if entry is not None and entry.accepted_findings:
        print(f"  previously accepted: {', '.join(entry.accepted_findings)}")
    return 2 if skill_scan.criticals(findings) else 0


def cmd_skill_trust(name: str, *, revoke: bool = False) -> int:
    """`ava skill trust <name> [--revoke]` — record that a human read this
    package's content and vouches for it (tier "reviewed"), or take that back.

    Deliberately not something an install can do for you: "reviewed" is the one
    tier that asserts a person looked, and it is what the recall layer consults
    before pulling a skill's text into an agent's context unprompted. Read the
    package (`ava skill scan <name>` shows what the scanner saw) first.
    """
    from shared import install_registry

    entry = install_registry.get(name)
    if entry is None:
        print(
            f"[ava skill trust] '{name}' is not tracked in the install registry.", file=sys.stderr
        )
        return 1
    if entry.trust == "builtin":
        print(
            f"[ava skill trust] '{name}' is builtin (converge-managed from this checkout); "
            "its trust tier is not yours to set.",
            file=sys.stderr,
        )
        return 1
    entry.trust = "unreviewed" if revoke else "reviewed"
    entry.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    install_registry.register(entry)
    print(f"[ava skill trust] {name} -> trust={entry.trust}.")
    return 0


def cmd_skill_enable(name: str) -> int:
    """`ava skill enable <name>` — surface a tracked package to the skill scanner."""
    return _set_enabled(name, enabled=True)


def cmd_skill_disable(name: str) -> int:
    """`ava skill disable <name>` — hide a tracked package (stays on disk)."""
    return _set_enabled(name, enabled=False)


def _set_enabled(name: str, *, enabled: bool) -> int:
    from shared import install_registry
    from shared.skill_names import find

    verb = "enable" if enabled else "disable"
    with install_registry.mutate() as registry:
        # The typed name is inbound: fold it onto the row's real spelling so the
        # canonical dash form works against a registry row still written with
        # underscores (and vice versa).
        tracked = find(name, (p.name for p in registry.packages))
        entry = next((p for p in registry.packages if p.name == tracked), None)
        if entry is None:
            known = ", ".join(sorted(p.name for p in registry.packages)) or "(none)"
            print(
                f"[ava skill {verb}] '{name}' is not tracked (known: {known}). "
                f"An untracked dir under the skills dir needs `ava skill register {name}` first.",
                file=sys.stderr,
            )
            return 1
        entry.enabled = enabled
    print(f"[ava skill {verb}] {name} {verb}d.")
    print("  takes effect on the next skill scan; no restart needed.")
    return 0


def cmd_skill_register(name: str, *, accept_risk: bool = False) -> int:
    """`ava skill register <name> [--accept-risk]` — track
    `$AVA_HOME/skills/<name>/` as a user package so the scanner loads it. The
    dir must already exist and every SKILL.md inside must parse (fail here, not
    in every agent's scan).

    A hand-copied dir is third-party content that never passed the install gate,
    so it is scanned here — otherwise `cp -r && ava skill register` would be the
    way around every check `ava skill install` makes.
    """
    from shared import install_registry, paths
    from shared.skill_index import SkillFormatError
    from shared.skill_index import parse_skill_frontmatter as _parse_frontmatter
    from shared.skill_names import find

    from ._skill_package import SkillScanRefused, scan_report

    # `name` is both a registry key and a path segment, so it has to become the
    # directory's REAL spelling before either use: the caller may type the
    # canonical dash form at a directory that is still `foo_bar/` on disk.
    skills_root = paths.skills_dir()
    on_disk = (
        find(name, (p.name for p in skills_root.iterdir() if p.is_dir()))
        if skills_root.is_dir()
        else None
    )
    d = skills_root / (on_disk or name)
    if not d.is_dir():
        print(f"[ava skill register] no dir at {d}.", file=sys.stderr)
        return 1
    name = d.name
    if install_registry.get(name) is not None:
        print(
            f"[ava skill register] '{name}' is already tracked; "
            f"`ava skill enable {name}` if it is disabled.",
            file=sys.stderr,
        )
        return 1
    skill_mds = sorted(d.rglob("SKILL.md"))
    if not skill_mds:
        print(f"[ava skill register] no SKILL.md anywhere under {d}.", file=sys.stderr)
        return 1
    for skill_md in skill_mds:
        try:
            _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except SkillFormatError as e:
            print(f"[ava skill register] bad {skill_md}: {e}", file=sys.stderr)
            return 1
    try:
        report, accepted = scan_report(d, name, accept_risk=accept_risk)
    except SkillScanRefused as e:
        print("[ava skill register] security scan found critical patterns:", file=sys.stderr)
        print(e.report, file=sys.stderr)
        print(_REFUSAL_ADVICE.replace("install", "register"), file=sys.stderr)
        return 1
    now = datetime.now(UTC).isoformat(timespec="seconds")
    install_registry.register(
        install_registry.InstalledPackage(
            name=name,
            type="skill",
            installed_at=now,
            updated_at=now,
            trust="unreviewed",
            scanned_at=now,
            accepted_findings=accepted,
        )
    )
    print(f"[ava skill register] '{name}' registered (origin=user, trust=unreviewed).")
    print(report)
    print("  active on the next skill scan; no restart needed.")
    return 0


# ─── update / upgrade (R5 design, task #1013) ───────────────────────────────


def _repo_native_sources(repo: Path | None = None) -> list[_Source]:
    """The repo-native skill sources of the running checkout (builtin skills,
    `.agents/skills` project skills, builtin-plugin skills) — the set `ava
    skill update` syncs. Installed-plugin skills are excluded (they update via
    `ava plugins upgrade`)."""
    from ._converge_skills import iter_sources
    from ._repo import _repo_root

    sources, _conflicts = iter_sources(repo or _repo_root())
    return [s for s in sources if s.bootstrap_only]


def _land_repo_copy(
    s: _Source,
    skills_root: Path,
    registry: reg.Registry,
    now: str,
    *,
    overwrite: bool,
) -> str:
    """Land / overwrite `skills_root/<name>` from the source and (re)track it
    as repo-native. Returns the package name (the caller files it under the
    outcome it already knows)."""
    from shared.install_registry import InstalledPackage, tree_hash

    from ._converge_skills import _copy_tree

    dest = skills_root / s.name
    if not dest.exists() or overwrite:
        _copy_tree(s.src, dest)
    entry = next((p for p in registry.packages if p.name == s.name), None)
    if entry is None:
        entry = InstalledPackage(name=s.name, type="skill", installed_at=now)
        registry.packages.append(entry)
    entry.origin = s.origin
    entry.origin_path = str(s.src)
    entry.source = None
    entry.path = None
    entry.ref = None
    entry.trust = "builtin"
    entry.content_hash = tree_hash(s.src)
    entry.updated_at = now
    if entry.installed_at is None:
        entry.installed_at = now
    return s.name


def _update_one(
    s: _Source,
    skills_root: Path,
    registry: reg.Registry,
    now: str,
    *,
    force: bool,
    landed: list[str],
    updated: list[str],
    unchanged: list[str],
    conflicts: list[str],
) -> None:
    """Bring one repo-native skill in line with its source (the per-package
    update/conflict/force decision table of `cmd_skill_update`)."""
    from shared.install_registry import tree_hash

    dest = skills_root / s.name
    src_hash = tree_hash(s.src)
    skip = reg.preserved_subpaths(dest)
    entry = next((p for p in registry.packages if p.name == s.name), None)

    if entry is not None and entry.origin == "user":
        if entry.source and entry.source.startswith(
            (".agents/skills/", ".claude/skills/", ".ava/skills/")
        ):
            # Hand-installed residue of this repo skill (the pre-converge way
            # to get project skills into the load dir). Adopt it.
            if not dest.exists():
                landed.append(_land_repo_copy(s, skills_root, registry, now, overwrite=False))
                return
            if tree_hash(dest, skip_subtrees=skip) == src_hash:
                entry.origin = s.origin
                entry.origin_path = str(s.src)
                entry.source = None
                entry.path = None
                entry.ref = None
                # Adopting a repo skill's own content: it ships under the
                # checkout's review, so the builtin stamp applies (converge's
                # `_stamp_trust` sets it on its own adopt path; the update
                # residue-adopt path had been leaving trust=unreviewed —
                # audit round 2, skills-plugins #5).
                entry.trust = "builtin"
                entry.content_hash = src_hash
                entry.updated_at = now
                unchanged.append(s.name)
            elif force:
                updated.append(_land_repo_copy(s, skills_root, registry, now, overwrite=True))
            else:
                conflicts.append(
                    f"'{s.name}': local copy differs from the repo source (and the "
                    f"source changed) — re-run with --force to adopt the repo version"
                )
            return
        # A genuinely third-party or hand-registered package: converge shadows
        # it, and so does update.
        print(f"[ava skill update] '{s.name}' is a user-installed package; not updated.")
        return

    if not dest.exists():
        landed.append(_land_repo_copy(s, skills_root, registry, now, overwrite=False))
        return
    if entry is None:
        if tree_hash(dest, skip_subtrees=skip) == src_hash:
            # Lost registry row: adopt byte-identical copies (converge's
            # self-heal), warn on differing ones.
            _land_repo_copy(s, skills_root, registry, now, overwrite=False)
            unchanged.append(s.name)
        else:
            print(
                f"[ava skill update] '{s.name}': untracked dir {dest} shadows the repo "
                f"source (content differs); not updated (`ava skill register {s.name}` "
                f"to keep yours).",
                file=sys.stderr,
            )
        return

    # Tracked repo-native (or builtin-plugin) entry: the update path.
    dest_hash = tree_hash(dest, skip_subtrees=skip)
    if dest_hash == src_hash:
        # Content is current; re-anchor a stale recorded source (e.g. a row
        # written from a deleted worktree — audit 02 #15).
        if entry.origin_path != str(s.src):
            entry.origin_path, entry.updated_at = str(s.src), now
        unchanged.append(s.name)
    elif dest_hash == entry.content_hash or force:
        updated.append(_land_repo_copy(s, skills_root, registry, now, overwrite=True))
    else:
        conflicts.append(
            f"'{s.name}': modified locally (content differs from the last synced "
            f"version) and the source changed — re-run with --force to overwrite "
            f"your edits with the repo version"
        )


def cmd_skill_update(
    names: list[str] | None, *, force: bool = False, repo: Path | None = None
) -> int:
    """`ava skill update [name ...] [--force]` — bring repo-native skills (from
    this checkout) into the load dir.

    The explicit update for repo-native skills (R5 ruling: converge only lands
    missing copies, never updates). Per package:

    - copy absent            -> landed + tracked (origin=repo)
    - copy matches source    -> nothing to do
    - copy matches registry  -> updated to the source
      content_hash (no local edits)
    - copy differs from the  -> conflict: abort that package with the local
      recorded hash           edit surfaced; `--force` overwrites it
    - user-origin residue of a repo skill (installed by hand from
      `.agents/skills` before converge knew the source) -> adopted as repo
      when content matches, conflict/force otherwise

    `repo` is the checkout whose sources to sync from (tests inject a
    synthetic tree; default = this checkout).
    """
    from datetime import UTC, datetime

    from shared import install_registry as reg
    from shared import paths

    from ._converge_skills import assert_repo_source_bound
    from ._repo import _repo_root

    repo = repo or _repo_root()
    try:
        assert_repo_source_bound(repo, Path(settings.general.ava_home).expanduser())
    except RuntimeError as e:
        print(f"[ava skill update] {e}", file=sys.stderr)
        return 1

    sources = _repo_native_sources(repo)
    if names:
        wanted = set(names)
        sources = [s for s in sources if s.name in wanted]
        missing = wanted - {s.name for s in sources}
        for m in sorted(missing):
            print(
                f"[ava skill update] '{m}' is not a repo-native skill of this checkout "
                f"(repo/plugin builtins and .agents/skills are; a user install would be "
                f"`ava skill upgrade {m}`).",
                file=sys.stderr,
            )
    skills_root = paths.skills_dir()
    skills_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    landed: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    with reg.mutate() as registry:
        for s in sources:
            _update_one(
                s,
                skills_root,
                registry,
                now,
                force=force,
                landed=landed,
                updated=updated,
                unchanged=unchanged,
                conflicts=conflicts,
            )
    for name in landed:
        print(f"[ava skill update] landed '{name}' -> {skills_root / name}")
    for name in updated:
        print(f"[ava skill update] updated '{name}'")
    for name in unchanged:
        print(f"[ava skill update] '{name}' up to date")
    for c in conflicts:
        print(f"[ava skill update] conflict: {c}", file=sys.stderr)
    if conflicts and not force:
        print(
            "  NONE were overwritten; re-run with --force to replace local edits.",
            file=sys.stderr,
        )
        return 1
    print("  active on the next skill scan; no restart needed.")
    return 0


def cmd_skill_upgrade(name: str, *, force: bool = False) -> int:
    """`ava skill upgrade <name> [--force]` — re-fetch an installed skill package
    from its recorded git source (a private skills repo and other user
    installs).

    A locally edited copy (content differs from what the last install/upgrade
    wrote) aborts with a conflict unless `--force` is given — the R5 conflict
    contract, mirroring `git pull`.
    """
    import shutil
    import subprocess

    from shared import install_registry, paths

    from ._pkg_source import SourcePathNotFoundError, acquire_source, cleanup_temp

    pkg = install_registry.get(name)
    if pkg is None or pkg.type != "skill":
        print(
            f"[ava skill upgrade] '{name}' is not an installed skill package "
            f"(a repo-native skill updates via `ava skill update {name}`).",
            file=sys.stderr,
        )
        return 1
    if pkg.source is None:
        print(
            f"[ava skill upgrade] '{name}' has no recorded source "
            f"(a hand-registered local skill is not updatable by design).",
            file=sys.stderr,
        )
        return 1

    dest = paths.skills_dir() / name
    if dest.exists() and not force and install_registry.copy_changed(dest, pkg.installed_hash):
        print(
            f"[ava skill upgrade] '{name}' was modified locally; refusing to overwrite. "
            f"Re-run with --force to replace your changes with the fetched source.",
            file=sys.stderr,
        )
        return 1

    try:
        acquired = acquire_source(pkg.source, pkg.ref)
    except (subprocess.CalledProcessError, SourcePathNotFoundError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"[ava skill upgrade] {detail}", file=sys.stderr)
        return 1
    try:
        pkg_dir = acquired.root / pkg.path if pkg.path else acquired.root
        if not (pkg_dir / "SKILL.md").is_file():
            print(
                "[ava skill upgrade] source no longer has SKILL.md at the package root; aborting.",
                file=sys.stderr,
            )
            return 1
        if dest.exists():
            shutil.rmtree(dest)
        if acquired.cloned is not None:
            shutil.rmtree(pkg_dir / ".git", ignore_errors=True)
            shutil.move(str(pkg_dir), str(dest))
        else:
            # A local-path source is read in place (never moved) — install's
            # contract, which upgrade used to break: moving a local source
            # relocated the user's own directory out from under them and
            # deleted its .git. Copy, dropping VCS/venv residue like the clone
            # path does.
            shutil.copytree(pkg_dir, dest, ignore=shutil.ignore_patterns(".git", ".venv"))

        from datetime import UTC, datetime

        pkg.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        pkg.installed_hash = install_registry.tree_hash(dest)
        install_registry.register(pkg)
        print(f"[ava skill upgrade] re-fetched '{name}' from {pkg.source}.")
        print("  active on the next skill scan; no restart needed.")
        return 0
    finally:
        cleanup_temp(acquired.cloned)
