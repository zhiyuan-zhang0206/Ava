"""`ava plugins` subcommands.

- `update`              — auto-merge plugin config disk image schema diff
                          (logic in `base.packages.plugins.enable_config.update_all_disk_images`).
- `install <url>`       — install an external package from a git source and
                          record it in the install registry (`base.packages.extensions.install_registry`).
                          A bare **skill** (SKILL.md at the package root) lands in
                          the `~/.ava/skills/` load dir — `ava skill install` is the
                          fuller skill entry point (local paths + skill collections);
                          a **Claude Code plugin**
                          (`.claude-plugin/plugin.json`) is materialized into
                          `~/.ava/plugins/` via `_claude_code_plugin` — its
                          `agents/` become one orchestrator skill, its bundled
                          `.mcp.json` is carried over for the MCP loader, and its
                          skills are synced into `~/.ava/skills/<name>/` by an
                          immediate converge pass (`skills_sync`). `--path`
                          selects a subdir of the source repo.
- `uninstall <name>`    — remove an installed package + its registry entry
                          (a plugin's converged skills copy included).
- `installed`           — list registry entries.
- `upgrade <name>`      — re-fetch an installed package from its recorded source.

Packages activate on the next skill scan (no restart). A plugin that bundles
neither agents nor an MCP (hooks-only / skills-only) is refused until those
pieces land.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from base.agents.context.clients import DatabaseFactory
from cli.commands.extensions._claude_code_plugin import Materialized
from cli.commands.extensions._manifest_gate import gate_refuses

from ._pkg_source import cleanup_temp, clone_git


def cmd_plugins_update() -> int:
    """`ava plugins update` — scan all plugins, auto-merge disk image schema diff.

    Actual scan + merge logic lives in `base.packages.plugins.enable_config.update_all_disk_images`;
    this function only formats the structured result for printing.
    """
    from base.packages.plugins.enable_config import update_all_disk_images

    result = update_all_disk_images()
    if not result.entries:
        print("[ava plugins update] no plugins found")
        return 0

    print(f"[ava plugins update] processing {len(result.entries)} plugin(s)")
    any_error = False
    for entry in result.entries:
        if entry.status == "no_diff":
            print(f"  ✓ {entry.name}: no schema diff")
        elif entry.status == "skipped":
            print(f"  · {entry.name}: skipped ({entry.detail})")
        elif entry.status == "updated":
            details: list[str] = []
            if entry.added:
                details.append(f"added={entry.added}")
            if entry.removed:
                details.append(f"removed={entry.removed} (dropped from disk image)")
            print(f"  + {entry.name}: {', '.join(details)}")
        else:  # error
            any_error = True
            print(f"  ✗ {entry.name}: {entry.detail}", file=sys.stderr)
    return 1 if any_error else 0


def cmd_plugins_enable(name: str) -> int:
    """`ava plugins enable <name>` — enable a plugin in this machine's local config."""
    return _set_enabled(name, enabled=True)


def cmd_plugins_disable(name: str) -> int:
    """`ava plugins disable <name>` — disable a plugin in this machine's local config."""
    return _set_enabled(name, enabled=False)


def _set_enabled(name: str, *, enabled: bool) -> int:
    from base.packages.plugins.enable_config import DanglingPlugin, set_local_enabled

    verb = "enable" if enabled else "disable"
    try:
        set_local_enabled(name, enabled=enabled)
    except DanglingPlugin as e:
        print(f"[ava plugins {verb}] {e}", file=sys.stderr)
        return 1
    print(f"[ava plugins {verb}] {name} {verb}d in this machine's local config.")
    print("  takes effect on the next agent graph step; no restart needed.")
    return 0


def _skill_name_at(pkg_dir: Path) -> str | None:
    """Return the skill's frontmatter `name` if `pkg_dir` is a skill package
    (SKILL.md at its root), else None.

    Raises:
        SkillFormatError: SKILL.md is present but unparseable.
    """
    from base.packages.skills.index import parse_skill_frontmatter as _parse_frontmatter

    skill_md = pkg_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    fields, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    return fields["name"]


def _sync_skills_load_dir() -> None:
    """Run the skills converge pass so a just-(un)installed plugin's skills
    land in / leave `~/.ava/skills/` now, keeping the "active on the next
    skill scan, no restart" promise. Idempotent."""
    from base.paths import ava_home
    from cli.commands._repo import _repo_root
    from cli.commands.extensions.skills_sync import converge_skills

    result = converge_skills(_repo_root(), ava_home())
    for warning in result.warnings:
        print(f"  ! skills: {warning}", file=sys.stderr)


def _report_refusal(report: str) -> int:
    """Print a refused scan and the override advice; always returns exit code 1."""
    print("[ava plugins install] security scan found critical patterns:", file=sys.stderr)
    print(report, file=sys.stderr)
    print(
        "  Refusing to install. Read the package yourself, then re-run with "
        "--accept-risk if you still want it.",
        file=sys.stderr,
    )
    return 1


def _install_bare_skill(
    pkg_dir: Path,
    name: str,
    url: str,
    path: str | None,
    ref: str | None,
    *,
    accept_risk: bool,
    database_factory: DatabaseFactory,
) -> int:
    """Install a cloned bare-skill package into the load dir and register it.

    Shares the copy + registry write — and therefore the security scan — with
    `ava skill install`, so both entry points land a skill on disk the same way.
    """
    from .skill_package import SkillPackage, SkillPackageError, SkillScanRefused, install

    try:
        ((dest, report),) = install(
            [SkillPackage(root=pkg_dir, name=name)],
            source=url,
            path=path,
            ref=ref,
            accept_risk=accept_risk,
            database_factory=database_factory,
        )
    except SkillScanRefused as e:
        return _report_refusal(e.report)
    except SkillPackageError as e:
        print(f"[ava plugins install] {e}", file=sys.stderr)
        return 1
    print(f"[ava plugins install] installed skill '{name}' -> {dest}")
    print(report)
    print("  active on the next skill scan; no restart needed.")
    return 0


def _register_plugin_install(
    name: str,
    url: str | None,
    path: str | None,
    ref: str | None,
    *,
    accepted: list[str],
    dest: Path,
) -> None:
    """Record a freshly materialized plugin in the install registry.

    Install-failure cleanup (2026-08-28 ava_ledger discipline): if the
    registry write fails, the landed dir is removed again, so a retry does
    not hit "already installed" against an untracked copy.
    """
    from base.packages.extensions.install_registry import tree_hash

    from . import skill_package

    try:
        skill_package.register_installed(
            name,
            "plugin",
            url,
            path,
            ref,
            accepted_findings=accepted,
            content_hash=tree_hash(dest),
        )
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _atomic_plugin_replace(dest: Path, materialize: Callable[[], object]) -> None:
    """Replace the plugin at `dest` with a freshly materialized copy, atomically.

    The previous version is renamed aside, the new one is materialized into a
    staging dir and renamed into place, and only then is the backup deleted.
    On any failure the previous version is restored — the plugin on disk is
    always either the previous complete version or the new complete version,
    never a half-installed tree (2026-08-28 ava_ledger incident).
    """
    # Sweep residue from earlier hard-killed runs first — at this point every
    # dot-prefixed sibling is stale (the live backup is created below).
    from . import _claude_code_plugin

    _claude_code_plugin.sweep_plugin_residue(dest.parent)
    backup = dest.parent / f".{dest.name}.backup-{os.getpid()}"
    if dest.exists():
        dest.replace(backup)
    try:
        materialize()
    except BaseException:
        if backup.exists():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            backup.replace(dest)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _install_claude_code_plugin(
    pkg_dir: Path, url: str, path: str | None, ref: str | None, *, accept_risk: bool
) -> int:
    """Scan, materialize and register a Claude Code plugin bundle; prints what it contributes."""
    from base import paths

    from . import _claude_code_plugin, skill_package

    try:
        report, accepted = skill_package.scan_report(pkg_dir, pkg_dir.name, accept_risk=accept_risk)
    except skill_package.SkillScanRefused as e:
        return _report_refusal(e.report)
    _claude_code_plugin.sweep_plugin_residue(paths.plugins_dir())
    try:
        result = _claude_code_plugin.materialize(pkg_dir, paths.plugins_dir())
    except _claude_code_plugin.ClaudeCodePluginError as e:
        print(f"[ava plugins install] {e}", file=sys.stderr)
        return 1
    _register_plugin_install(
        result.name,
        url,
        path,
        ref,
        accepted=accepted,
        dest=paths.plugins_dir() / result.name,
    )
    _sync_skills_load_dir()
    _print_plugin_install(result, report)
    return 0


def _print_plugin_install(result: Materialized, report: str) -> None:
    from base import paths

    print(
        f"[ava plugins install] installed plugin '{result.name}' "
        f"-> {paths.plugins_dir() / result.name}"
    )
    print(report)
    print("  contributes:")
    if result.shipped_skills:
        skills = ", ".join(result.shipped_skills)
        print(
            f"    {len(result.shipped_skills)} skill(s): {skills} — "
            "active on the next skill scan, no restart."
        )
    if result.skill_name:
        agents = ", ".join(result.agents)
        print(
            f"    skill '{result.skill_name}' ({len(result.agents)} review "
            f"agent(s): {agents}) — active on the next skill scan, no restart."
        )
    if result.commands:
        cmds = ", ".join(f"/{c}" for c in result.commands)
        print(f"    {len(result.commands)} command(s): {cmds} — available in the composer.")
    if result.mcp_servers:
        servers = ", ".join(result.mcp_servers)
        print(f"    MCP server(s): {servers} — connect on next use.")


def cmd_plugins_install(
    url: str,
    ref: str | None,
    path: str | None,
    *,
    accept_risk: bool = False,
    database_factory: DatabaseFactory,
) -> int:
    """`ava plugins install <url> [--path SUBDIR] [--ref REF] [--accept-risk]` —
    install an external package.

    A plugin is strictly more dangerous than a skill — its hooks and MCP servers
    run code rather than being read — so the same scan gates it, over the whole
    bundle rather than just the skills it ships.
    """

    from . import _claude_code_plugin

    try:
        cloned = clone_git(url, ref)
    except subprocess.CalledProcessError as e:
        print(f"[ava plugins install] git failed: {e.stderr.strip()}", file=sys.stderr)
        return 1

    try:
        pkg_dir = cloned / path if path else cloned
        if not pkg_dir.is_dir():
            print(f"[ava plugins install] path '{path}' not found in source.", file=sys.stderr)
            return 1

        if gate_refuses(pkg_dir, command="plugins install"):
            return 1

        from base.packages.skills.index import SkillFormatError

        try:
            name = _skill_name_at(pkg_dir)
        except SkillFormatError as e:
            print(f"[ava plugins install] bad SKILL.md: {e}", file=sys.stderr)
            return 1

        if name is not None:
            return _install_bare_skill(
                pkg_dir,
                name,
                url,
                path,
                ref,
                accept_risk=accept_risk,
                database_factory=database_factory,
            )

        if _claude_code_plugin.is_claude_code_plugin(pkg_dir):
            return _install_claude_code_plugin(pkg_dir, url, path, ref, accept_risk=accept_risk)

        if (pkg_dir / ".mcp.json").is_file():
            print(
                "[ava plugins install] this looks like a standalone MCP package "
                "(.mcp.json at the package root). Install it with `ava mcp install`.",
                file=sys.stderr,
            )
            return 1

        print(
            "[ava plugins install] unrecognized package: no SKILL.md and no "
            ".claude-plugin/plugin.json at the package root. A repo that is a "
            "collection of skills (skills/ or .claude/skills/) installs with "
            "`ava skill install`.",
            file=sys.stderr,
        )
        return 1
    finally:
        cleanup_temp(cloned)


def _install_dest(pkg_type: str, name: str) -> Path:
    """On-disk install location for a package, by type."""
    from base import paths

    return (paths.plugins_dir() if pkg_type == "plugin" else paths.skills_dir()) / name


def cmd_plugins_uninstall(name: str) -> int:
    """`ava plugins uninstall <name>` — remove an installed package + registry entry."""
    from base import paths
    from base.packages.extensions import install_registry

    pkg = install_registry.get(name)
    if pkg is None:
        print(
            f"[ava plugins uninstall] '{name}' is not tracked in the install registry.",
            file=sys.stderr,
        )
        return 1
    dest = _install_dest(pkg.type, name)
    if dest.exists():
        shutil.rmtree(dest)
    if pkg.type == "plugin":
        # Its skills were converged into the load dir; drop that copy too.
        converged = paths.skills_dir() / name
        if converged.exists():
            shutil.rmtree(converged)
        # Sweep atomic-install residue so nothing ghost-like survives an
        # uninstall (QA N4, #880 review).
        from . import _claude_code_plugin

        _claude_code_plugin.sweep_plugin_residue(dest.parent)
    install_registry.deregister(name)
    print(f"[ava plugins uninstall] removed '{name}'.")
    return 0


def cmd_plugins_installed() -> int:
    """`ava plugins installed` — list install-registry entries."""
    from base.packages.extensions import install_registry

    pkgs = install_registry.load().packages
    if not pkgs:
        print("[ava plugins installed] (none)")
        return 0
    for p in sorted(pkgs, key=lambda x: x.name):
        flag = "enabled" if p.enabled else "disabled"
        ref = p.ref or "(default)"
        print(f"  {p.name}  [{p.type}, {flag}]  {p.source} @ {ref}")
    return 0


def cmd_plugins_upgrade(name: str) -> int:
    """`ava plugins upgrade <name>` — re-fetch an installed package
    from its source.

    A locally edited copy (content differs from what the last install/upgrade
    wrote) is replaced, and the replacement is reported.
    """
    from base import paths
    from base.packages.extensions import install_registry

    from . import _claude_code_plugin

    pkg = install_registry.get(name)
    if pkg is None:
        print(f"[ava plugins upgrade] '{name}' is not tracked.", file=sys.stderr)
        return 1
    if pkg.source is None:
        print(
            f"[ava plugins upgrade] '{name}' has no recorded git source "
            f"(converge-managed, origin={pkg.origin}); it updates via the fleet update.",
            file=sys.stderr,
        )
        return 1

    dest = _install_dest(pkg.type, name)
    if dest.exists() and install_registry.copy_changed(dest, pkg.installed_hash):
        print(
            f"[ava plugins upgrade] note: '{name}' was modified locally; "
            "replacing it with the fetched source."
        )

    try:
        cloned = clone_git(pkg.source, pkg.ref)
    except subprocess.CalledProcessError as e:
        print(f"[ava plugins upgrade] git failed: {e.stderr.strip()}", file=sys.stderr)
        return 1

    try:
        pkg_dir = cloned / pkg.path if pkg.path else cloned
        if not pkg_dir.is_dir():
            print(
                f"[ava plugins upgrade] path '{pkg.path}' no longer in source; aborting.",
                file=sys.stderr,
            )
            return 1
        if pkg.type == "plugin":
            try:
                _atomic_plugin_replace(
                    dest,
                    lambda: _claude_code_plugin.materialize(pkg_dir, paths.plugins_dir()),
                )
            except _claude_code_plugin.ClaudeCodePluginError as e:
                print(f"[ava plugins upgrade] {e}", file=sys.stderr)
                return 1
        else:
            if not (pkg_dir / "SKILL.md").is_file():
                print(
                    "[ava plugins upgrade] source no longer has SKILL.md at the package "
                    "root; aborting.",
                    file=sys.stderr,
                )
                return 1
            shutil.rmtree(pkg_dir / ".git", ignore_errors=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(pkg_dir), str(dest))

        from datetime import UTC, datetime

        pkg.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        pkg.installed_hash = install_registry.tree_hash(dest)
        install_registry.register(pkg)
        if pkg.type == "plugin":
            # After the registry write — the converge pass updates this
            # entry's content_hash, which a later register(pkg) would clobber.
            _sync_skills_load_dir()
        print(f"[ava plugins upgrade] re-fetched '{name}' from {pkg.source}.")
        print("  active on the next skill scan; no restart needed.")
        return 0
    finally:
        cleanup_temp(cloned)
